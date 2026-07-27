#!/usr/bin/env python3
"""
clean_node.py - Evacuate non-excluded pods from a single k8s node, after
verifying that every node in that node's rack is cordoned.

Usage:
    python3 clean_node.py ahab1101
    python3 clean_node.py ahab2201 --dry-run
    python3 clean_node.py ahab1201 --timeout 900 --poll-interval 15

IMPORTANT: this script only ever deletes pods running on the single node
passed as an argument. The rack-wide cordon check is a *safety gate*, not
a scope expansion.
"""

import argparse
import json
import re
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# CONFIG - edit these lists as your environment requires
# ---------------------------------------------------------------------------

# Namespaces that must NEVER be touched by this script (drained separately)
EXCLUDED_NAMESPACES = {
    "rook-ceph",
    "vault",
}

# Namespaces where pods can be deleted together as a batch instead of
# one-by-one. Still waits for the whole batch to be Ready before moving on.
BATCH_NAMESPACES = {
    "fate-shared",
}

RACK_NODE_REGEX = re.compile(r"^(ahab\d{2})\d{2}$")
LABELS_TO_STRIP = {"pod-template-hash", "controller-revision-hash", "statefulset.kubernetes.io/pod-name"}

DEFAULT_TIMEOUT = 600       # seconds to wait for a pod/group to come back Ready
DEFAULT_POLL_INTERVAL = 10  # seconds between readiness polls


# ---------------------------------------------------------------------------
# kubectl helpers
# ---------------------------------------------------------------------------

def run_kubectl_json(args):
    cmd = ["kubectl"] + args + ["-o", "json"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[ERROR] kubectl command failed: {' '.join(cmd)}")
        print(result.stderr.strip())
        sys.exit(1)
    return json.loads(result.stdout)


def run_kubectl(args, check=True):
    cmd = ["kubectl"] + args
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        print(f"[ERROR] kubectl command failed: {' '.join(cmd)}")
        print(result.stderr.strip())
        sys.exit(1)
    return result


# ---------------------------------------------------------------------------
# Rack / cordon verification
# ---------------------------------------------------------------------------

def get_rack_prefix(node_name):
    m = RACK_NODE_REGEX.match(node_name)
    if not m:
        print(f"[ERROR] Node name '{node_name}' does not match expected pattern "
              f"'ahabRRNN' (e.g. ahab1101, ahab2215).")
        sys.exit(1)
    return m.group(1)


def get_rack_nodes(rack_prefix):
    data = run_kubectl_json(["get", "nodes"])
    pattern = re.compile(rf"^{re.escape(rack_prefix)}\d{{2}}$")
    return [n["metadata"]["name"] for n in data["items"] if pattern.match(n["metadata"]["name"])]


def get_node_cordon_status(node_name):
    data = run_kubectl_json(["get", "node", node_name])
    return bool(data.get("spec", {}).get("unschedulable", False))


def verify_rack_cordoned(target_node):
    rack_prefix = get_rack_prefix(target_node)
    rack_nodes = get_rack_nodes(rack_prefix)

    if target_node not in rack_nodes:
        print(f"[ERROR] Target node {target_node} was not found in the cluster "
              f"(or does not match rack prefix {rack_prefix}).")
        sys.exit(1)

    print(f"[INFO] Rack prefix: {rack_prefix} -> {len(rack_nodes)} node(s) found: "
          f"{', '.join(sorted(rack_nodes))}")

    not_cordoned = []
    for node in sorted(rack_nodes):
        cordoned = get_node_cordon_status(node)
        print(f"    {node}: {'CORDONED' if cordoned else 'SCHEDULABLE'}")
        if not cordoned:
            not_cordoned.append(node)

    if not_cordoned:
        print(f"\n[ABORT] The following node(s) on rack {rack_prefix} are NOT cordoned: "
              f"{', '.join(not_cordoned)}")
        print("[ABORT] Cordon every node on this rack before running this script.")
        sys.exit(1)

    print(f"[OK] All {len(rack_nodes)} nodes on rack {rack_prefix} are cordoned.\n")


# ---------------------------------------------------------------------------
# Pod discovery / grouping
# ---------------------------------------------------------------------------

def get_pods_on_node(node_name):
    data = run_kubectl_json([
        "get", "pods", "--all-namespaces",
        "--field-selector", f"spec.nodeName={node_name}",
    ])
    return data["items"]


def pod_owner_kind(pod):
    owners = pod["metadata"].get("ownerReferences", [])
    if not owners:
        return None
    return owners[0]["kind"]


def build_selector(pod):
    labels = pod["metadata"].get("labels", {})
    filtered = {k: v for k, v in labels.items() if k not in LABELS_TO_STRIP}
    if not filtered:
        return None
    return ",".join(f"{k}={v}" for k, v in sorted(filtered.items()))


def group_pods_by_namespace(pods):
    grouped = {}
    for pod in pods:
        ns = pod["metadata"]["namespace"]
        grouped.setdefault(ns, []).append(pod)
    return grouped


def group_pods_by_selector(pods):
    """Within a namespace, group pods by their (owner-stripped) label selector."""
    groups = {}
    for pod in pods:
        sel = build_selector(pod)
        key = sel if sel else f"__nosel__/{pod['metadata']['name']}"
        groups.setdefault(key, []).append(pod)
    return groups


def is_pod_ready(pod):
    if pod["metadata"].get("deletionTimestamp"):
        return False
    if pod["status"].get("phase") != "Running":
        return False
    for cond in pod["status"].get("conditions", []):
        if cond["type"] == "Ready":
            return cond["status"] == "True"
    return False


# ---------------------------------------------------------------------------
# Readiness waiting
# ---------------------------------------------------------------------------

def get_selector_ready_count(namespace, selector, exclude_node=None):
    data = run_kubectl_json(["get", "pods", "-n", namespace, "-l", selector])
    ready = 0
    on_excluded = 0
    for pod in data["items"]:
        if is_pod_ready(pod):
            ready += 1
            if exclude_node and pod["spec"].get("nodeName") == exclude_node:
                on_excluded += 1
    return ready, on_excluded


def wait_for_ready(namespace, selector, target_ready_count, exclude_node,
                    timeout, poll_interval, label=""):
    print(f"    Waiting for {label or selector} in ns/{namespace} "
          f"to reach {target_ready_count} Ready pod(s) off {exclude_node} "
          f"(timeout {timeout}s)...")
    start = time.time()
    while time.time() - start < timeout:
        ready, on_excluded = get_selector_ready_count(namespace, selector, exclude_node)
        if ready >= target_ready_count and on_excluded == 0:
            print(f"    [OK] {ready} pod(s) Ready, none on {exclude_node}.")
            return True
        print(f"    ... ready={ready}/{target_ready_count}, still on {exclude_node}={on_excluded} "
              f"(elapsed {int(time.time() - start)}s)")
        time.sleep(poll_interval)

    print(f"    [TIMEOUT] {label or selector} in ns/{namespace} did not reach "
          f"{target_ready_count} Ready pods off {exclude_node} within {timeout}s.")
    return False


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------

def confirm(prompt):
    answer = input(f"{prompt} [y/N]: ").strip().lower()
    return answer == "y"


# ---------------------------------------------------------------------------
# Core migration logic
# ---------------------------------------------------------------------------

def process_namespace(namespace, pods, target_node, dry_run, timeout, poll_interval):
    print(f"\n===== Namespace: {namespace} ({len(pods)} pod(s) on {target_node}) =====")

    # Filter out DaemonSet pods and unowned/bare pods up front
    workable = []
    for pod in pods:
        name = pod["metadata"]["name"]
        owner = pod_owner_kind(pod)
        if owner == "DaemonSet":
            print(f"  [SKIP] {name}: owned by DaemonSet, will not be migrated by this script.")
            continue
        if owner is None:
            print(f"  [SKIP] {name}: no owning controller (bare pod) - deleting it would "
                  f"NOT recreate it. Handle manually.")
            continue
        workable.append(pod)

    if not workable:
        print("  Nothing to migrate in this namespace.")
        return

    batch_mode = namespace in BATCH_NAMESPACES
    selector_groups = group_pods_by_selector(workable)

    for sel_key, group_pods in selector_groups.items():
        if sel_key.startswith("__nosel__/"):
            print(f"  [SKIP] {group_pods[0]['metadata']['name']}: no usable labels to "
                  f"build a selector, skipping to be safe.")
            continue

        selector = sel_key
        owner_kind = pod_owner_kind(group_pods[0])
        owner_name = group_pods[0]["metadata"]["ownerReferences"][0]["name"]
        label = f"{owner_kind}/{owner_name}"

        # Baseline ready count for this selector across the WHOLE namespace
        # (includes pods on this node and elsewhere) BEFORE we delete anything.
        baseline_ready, _ = get_selector_ready_count(namespace, selector)

        if batch_mode:
            pod_names = [p["metadata"]["name"] for p in group_pods]
            print(f"\n  -- Batch group {label}: {len(pod_names)} pod(s) on {target_node}: "
                  f"{', '.join(pod_names)}")
            if dry_run:
                print(f"  [DRY-RUN] Would delete {len(pod_names)} pod(s) as a batch and "
                      f"wait for {baseline_ready} Ready off {target_node}.")
                continue
            if not confirm(f"  Delete {len(pod_names)} pod(s) in {label} as a batch?"):
                print("  Skipped by user.")
                continue
            run_kubectl(["delete", "pod", "-n", namespace] + pod_names, check=False)
            ok = wait_for_ready(namespace, selector, baseline_ready, target_node,
                                 timeout, poll_interval, label=label)
            if not ok:
                print(f"[ABORT] Group {label} in ns/{namespace} failed to come back Ready. Stopping.")
                sys.exit(1)
        else:
            for pod in group_pods:
                pod_name = pod["metadata"]["name"]
                print(f"\n  -- {label}: pod {pod_name} on {target_node}")
                if dry_run:
                    print(f"  [DRY-RUN] Would delete {pod_name} and wait for "
                          f"{baseline_ready} Ready off {target_node}.")
                    continue
                if not confirm(f"  Delete pod {pod_name}?"):
                    print("  Skipped by user.")
                    continue
                run_kubectl(["delete", "pod", "-n", namespace, pod_name], check=False)
                ok = wait_for_ready(namespace, selector, baseline_ready, target_node,
                                     timeout, poll_interval, label=f"{label} ({pod_name})")
                if not ok:
                    print(f"[ABORT] {pod_name} in ns/{namespace} failed to come back Ready. Stopping.")
                    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Migrate pods off a single cordoned node, "
                                                   "after verifying its whole rack is cordoned.")
    parser.add_argument("node", help="Target node, e.g. ahab1101")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen, no deletions.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                         help=f"Seconds to wait for readiness per pod/group (default {DEFAULT_TIMEOUT}).")
    parser.add_argument("--poll-interval", type=int, default=DEFAULT_POLL_INTERVAL,
                         help=f"Seconds between readiness polls (default {DEFAULT_POLL_INTERVAL}).")
    args = parser.parse_args()

    target_node = args.node

    print(f"[INFO] Target node: {target_node}")
    print(f"[INFO] Excluded namespaces: {', '.join(sorted(EXCLUDED_NAMESPACES))}")
    print(f"[INFO] Batch namespaces: {', '.join(sorted(BATCH_NAMESPACES))}\n")

    # 1. Rack-wide cordon check (safety gate)
    verify_rack_cordoned(target_node)

    # 2. Discover pods on the target node only
    pods = get_pods_on_node(target_node)
    if not pods:
        print(f"[INFO] No pods found on {target_node}. Nothing to do.")
        return

    grouped = group_pods_by_namespace(pods)

    print("[INFO] Pods found on target node by namespace:")
    for ns, ns_pods in sorted(grouped.items()):
        tag = " (EXCLUDED - handled by separate script)" if ns in EXCLUDED_NAMESPACES else ""
        print(f"    {ns}: {len(ns_pods)} pod(s){tag}")

    if not args.dry_run:
        if not confirm(f"\nProceed with migrating pods off {target_node}?"):
            print("Aborted by user.")
            return

    # 3. Process each non-excluded namespace, one at a time
    for ns, ns_pods in sorted(grouped.items()):
        if ns in EXCLUDED_NAMESPACES:
            print(f"\n[SKIP] Namespace {ns} is excluded, skipping ({len(ns_pods)} pod(s)).")
            continue
        process_namespace(ns, ns_pods, target_node, args.dry_run, args.timeout, args.poll_interval)

    print(f"\n[DONE] Finished processing {target_node}.")


if __name__ == "__main__":
    main()
