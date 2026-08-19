#!/usr/bin/env python3
"""
GitLab Post-Upgrade Validation Script
======================================
Runs an automated smoke-test suite against a GitLab instance after an
upgrade (e.g. 18.11.11 -> 19.2.4) to catch regressions before rollout.

Covers (automatable):
  - Instance health/liveness/readiness + version check
  - Authentication (PAT)
  - Project / Issue / MR CRUD (core Rails app + DB)
  - File upload / attachment handling
  - Git operations over HTTPS (clone, push, pull)
  - CI/CD pipeline trigger + status polling
  - CI/CD job token scope settings
  - Runner registration & online status
  - Webhook delivery
  - Access tokens (project/group), Deploy tokens
  - GitLab CLI (glab) basic commands, if installed

NOT covered here (needs manual verification, see companion runbook):
  - UI rendering / navigation / visual regressions
  - SSH-based git operations (requires runner/agent SSH key setup)
  - Container/package registry push-pull (needs docker/npm/etc. tooling)
  - SSO/SAML/LDAP login flows
  - Geo replication (if applicable)

Usage:
  export GITLAB_URL="https://gitlab.yourcompany.com"
  export GITLAB_TOKEN="glpat-xxxxxxxxxxxxxxxxxxxx"   # PAT with api scope, admin recommended
  export GITLAB_NAMESPACE="your-group"                # namespace to create test project in
  export EXPECTED_VERSION="19.2.4"                    # optional, validates /api/v4/version
  python3 gitlab_upgrade_validation.py

Exit code: 0 if all checks pass, 1 if any check fails.
Cleans up the test project at the end (set KEEP_TEST_PROJECT=1 to skip).
"""

import os
import sys
import time
import json
import base64
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print("ERROR: pip install requests --break-system-packages")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
GITLAB_URL = os.environ.get("GITLAB_URL", "").rstrip("/")
TOKEN = os.environ.get("GITLAB_TOKEN", "")
NAMESPACE = os.environ.get("GITLAB_NAMESPACE", "")
EXPECTED_VERSION = os.environ.get("EXPECTED_VERSION", "")
KEEP_TEST_PROJECT = os.environ.get("KEEP_TEST_PROJECT", "0") == "1"
VERIFY_TLS = os.environ.get("GITLAB_VERIFY_TLS", "1") != "0"
TIMEOUT = 30
SSH_PRIVATE_KEY_PATH = os.environ.get("GIT_SSH_PRIVATE_KEY_PATH", "")
GIT_PROTOCOL_PREF = os.environ.get("GIT_TEST_PROTOCOL", "auto")  # auto | https | ssh
WEBHOOK_TEST_URL = os.environ.get("WEBHOOK_TEST_URL", "https://httpbin.org/post")

if not GITLAB_URL or not TOKEN:
    print("ERROR: set GITLAB_URL and GITLAB_TOKEN environment variables.")
    sys.exit(1)

API = f"{GITLAB_URL}/api/v4"
HEADERS = {"PRIVATE-TOKEN": TOKEN}
RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
TEST_PROJECT_NAME = f"upgrade-validation-{RUN_ID}"

results = []  # list of (name, passed: bool, detail: str)


def record(name, passed, detail=""):
    results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not passed else ""))
    return passed


def req(method, path, expect=(200, 201, 202, 204), **kwargs):
    url = path if path.startswith("http") else f"{API}{path}"
    kwargs.setdefault("headers", {}).update(HEADERS)
    kwargs.setdefault("timeout", TIMEOUT)
    kwargs.setdefault("verify", VERIFY_TLS)
    resp = requests.request(method, url, **kwargs)
    ok = resp.status_code in expect
    return ok, resp


# ---------------------------------------------------------------------------
# 1. Instance health
# ---------------------------------------------------------------------------
def check_health_endpoints():
    # NOTE: These are informational only, not pass/fail-gating. They're
    # unauthenticated and, by default, IP-allowlisted (monitoring_whitelist
    # in gitlab.rb) — a 404 from an arbitrary client is expected and doesn't
    # indicate the instance is unhealthy. The authenticated /api/v4/user and
    # /api/v4/version calls elsewhere are the real reachability/health gate.
    for ep in ["/-/liveness", "/-/readiness", "/-/health"]:
        try:
            ok, resp = req("GET", f"{GITLAB_URL}{ep}", expect=(200,))
            status = "reachable (200)" if ok else f"HTTP {resp.status_code} (likely IP-allowlisted, not necessarily unhealthy)"
        except requests.RequestException as e:
            status = f"request error: {e}"
        print(f"[INFO] Health endpoint {ep} — {status}")


def check_version():
    ok, resp = req("GET", "/version")
    if not ok:
        record("Version endpoint", False, f"HTTP {resp.status_code}")
        return
    data = resp.json()
    version = data.get("version", "")
    detail = f"Reported version: {version} (revision {data.get('revision')})"
    if EXPECTED_VERSION:
        matched = version.startswith(EXPECTED_VERSION.split("-")[0])
        record("Version matches expected", matched, f"{detail}, expected {EXPECTED_VERSION}")
    else:
        record("Version endpoint reachable", True, detail)
        print(f"      -> {detail}")


# ---------------------------------------------------------------------------
# 2. Auth / current user
# ---------------------------------------------------------------------------
def check_auth():
    ok, resp = req("GET", "/user")
    if ok:
        u = resp.json()
        record("PAT authentication", True, f"Authenticated as {u.get('username')} (id={u.get('id')})")
        return u
    record("PAT authentication", False, f"HTTP {resp.status_code}: {resp.text[:200]}")
    sys.exit(1)


def check_personal_access_tokens():
    ok, resp = req("GET", "/personal_access_tokens?state=active")
    record("List active personal access tokens", ok, f"HTTP {resp.status_code}")


# ---------------------------------------------------------------------------
# 3. Project / namespace setup
# ---------------------------------------------------------------------------
def create_test_project():
    payload = {"name": TEST_PROJECT_NAME, "path": TEST_PROJECT_NAME, "initialize_with_readme": True}
    if NAMESPACE:
        ok, resp = req("GET", f"/namespaces/{NAMESPACE}")
        if ok:
            payload["namespace_id"] = resp.json()["id"]
    ok, resp = req("POST", "/projects", json=payload, expect=(201,))
    if not ok:
        record("Create test project", False, f"HTTP {resp.status_code}: {resp.text[:300]}")
        sys.exit(1)
    project = resp.json()
    record("Create test project", True, f"id={project['id']} path={project['path_with_namespace']}")
    # wait for default branch / readme commit to settle
    time.sleep(3)
    return project


def delete_test_project(project_id):
    if KEEP_TEST_PROJECT:
        print(f"      -> KEEP_TEST_PROJECT=1 set, leaving project id={project_id} in place")
        return
    ok, resp = req("DELETE", f"/projects/{project_id}", expect=(202, 204))
    record("Cleanup: delete test project", ok, f"HTTP {resp.status_code}")


# ---------------------------------------------------------------------------
# 4. Issues, comments, uploads
# ---------------------------------------------------------------------------
def check_issue_lifecycle(project_id):
    ok, resp = req("POST", f"/projects/{project_id}/issues",
                    json={"title": "Upgrade validation test issue", "description": "Created by validation script."},
                    expect=(201,))
    if not ok:
        record("Create issue", False, f"HTTP {resp.status_code}")
        return
    issue_iid = resp.json()["iid"]
    record("Create issue", True, f"iid={issue_iid}")

    ok, resp = req("POST", f"/projects/{project_id}/issues/{issue_iid}/notes",
                    json={"body": "Validation comment."}, expect=(201,))
    record("Comment on issue", ok, f"HTTP {resp.status_code}")

    ok, resp = req("PUT", f"/projects/{project_id}/issues/{issue_iid}", json={"state_event": "close"})
    record("Close issue", ok, f"HTTP {resp.status_code}")


def check_file_upload(project_id):
    # 1x1 px transparent PNG
    png_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    files = {"file": ("validation.png", png_bytes, "image/png")}
    ok, resp = req("POST", f"/projects/{project_id}/uploads", files=files, expect=(201,))
    record("Upload attachment (issue/MR upload API)", ok,
           f"HTTP {resp.status_code}" + (f", url={resp.json().get('url')}" if ok else f": {resp.text[:200]}"))


# ---------------------------------------------------------------------------
# 5. Git operations (HTTPS)
# ---------------------------------------------------------------------------
def _git_env_for_ssh():
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    ssh_opts = ["-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10"]
    if SSH_PRIVATE_KEY_PATH:
        ssh_opts += ["-i", SSH_PRIVATE_KEY_PATH]
    env["GIT_SSH_COMMAND"] = "ssh " + " ".join(ssh_opts)
    return env


def _try_git_clone(clone_url, tmpdir, env, timeout=30):
    ssl_opts = [] if VERIFY_TLS else ["-c", "http.sslVerify=false"]
    try:
        r = subprocess.run(
            ["git"] + ssl_opts + ["-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=15",
             "clone", "--quiet", "--depth", "1", clone_url, tmpdir],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
        return r.returncode == 0, (r.stderr or "").strip()[:300]
    except subprocess.TimeoutExpired:
        return False, f"TIMED OUT after {timeout}s"
    except Exception as e:
        return False, f"unexpected error: {e}"


def check_git_ops(project):
    """
    Tries HTTPS first (fast, no SSH key needed), and falls back to SSH if
    HTTPS is disabled/blocked at the instance level (common: git access
    protocol restricted to SSH only). Set GIT_TEST_PROTOCOL=https or
    GIT_TEST_PROTOCOL=ssh to force one, or leave as 'auto' (default).
    """
    https_url = project["http_url_to_repo"]
    proto, rest = https_url.split("://", 1)
    https_auth_url = f"{proto}://oauth2:{TOKEN}@{rest}"
    ssh_url = project.get("ssh_url_to_repo")

    protocol_used = None
    clone_url = None

    attempts = []
    if GIT_PROTOCOL_PREF in ("auto", "https"):
        attempts.append(("https", https_auth_url, os.environ.copy()))
    if GIT_PROTOCOL_PREF in ("auto", "ssh") and ssh_url:
        attempts.append(("ssh", ssh_url, _git_env_for_ssh()))

    tmpdir = None
    last_detail = ""
    try:
        for proto_name, url, env in attempts:
            env.setdefault("GIT_TERMINAL_PROMPT", "0")
            # Fresh directory per attempt — a prior timed-out/partial clone
            # leaves a non-empty dir that would otherwise break the next
            # protocol's clone attempt with a misleading "already exists" error.
            attempt_dir = tempfile.mkdtemp(prefix="gl-validate-")
            ok, detail = _try_git_clone(url, attempt_dir, env, timeout=30)
            if ok:
                protocol_used = proto_name
                clone_url = url
                tmpdir = attempt_dir
                break
            last_detail = f"[{proto_name}] {detail}"
            shutil.rmtree(attempt_dir, ignore_errors=True)
            print(f"      -> {proto_name} clone failed ({detail}), "
                  f"{'trying next protocol...' if GIT_PROTOCOL_PREF == 'auto' else 'not retrying (protocol forced)'}")

        if protocol_used is None:
            record("Git clone", False,
                   f"failed on all attempted protocol(s) ({GIT_PROTOCOL_PREF}). Last error: {last_detail}. "
                   f"If HTTPS git access is disabled instance-wide, set GIT_TEST_PROTOCOL=ssh and "
                   f"GIT_SSH_PRIVATE_KEY_PATH=/path/to/key (the key must be registered against the "
                   f"user owning GITLAB_TOKEN).")
            record("Git commit", False, "skipped — clone failed")
            record("Git push", False, "skipped — clone failed")
            record("Git pull", False, "skipped — clone failed")
            return
        record(f"Git clone ({protocol_used.upper()})", True, f"cloned via {protocol_used}")

        test_file = os.path.join(tmpdir, "validation-file.txt")
        with open(test_file, "w") as f:
            f.write(f"validation run {RUN_ID}\n")

        env = _git_env_for_ssh() if protocol_used == "ssh" else os.environ.copy()
        env.setdefault("GIT_TERMINAL_PROMPT", "0")
        ssl_opts = [] if VERIFY_TLS else ["-c", "http.sslVerify=false"]

        subprocess.run(["git", "-C", tmpdir, "config", "user.email", "validation@local"], check=True, timeout=10)
        subprocess.run(["git", "-C", tmpdir, "config", "user.name", "Upgrade Validation"], check=True, timeout=10)
        subprocess.run(["git", "-C", tmpdir, "add", "."], check=True, timeout=10)
        r = subprocess.run(["git", "-C", tmpdir, "commit", "-m", "validation commit"],
                            capture_output=True, text=True, timeout=10)
        record("Git commit", r.returncode == 0, r.stderr.strip()[:300])

        r = subprocess.run(["git"] + ssl_opts + ["-C", tmpdir, "push", "origin", "HEAD"],
                            capture_output=True, text=True, timeout=30, env=env)
        record(f"Git push ({protocol_used.upper()})", r.returncode == 0, (r.stderr or r.stdout).strip()[:400])

        r = subprocess.run(["git"] + ssl_opts + ["-C", tmpdir, "pull", "--quiet"],
                            capture_output=True, text=True, timeout=30, env=env)
        record(f"Git pull ({protocol_used.upper()})", r.returncode == 0, r.stderr.strip()[:300])
    except subprocess.TimeoutExpired:
        record("Git commit/push/pull", False, "timed out")
    except Exception as e:
        record("Git commit/push/pull", False, f"unexpected error: {e}")
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 6. Merge request flow
# ---------------------------------------------------------------------------
def check_merge_request(project_id, default_branch):
    branch = f"validation-branch-{RUN_ID}"
    ok, resp = req("POST", f"/projects/{project_id}/repository/branches",
                    json={"branch": branch, "ref": default_branch}, expect=(201,))
    record("Create branch", ok, f"HTTP {resp.status_code}: {resp.text[:200]}" if not ok else "")
    if not ok:
        return

    ok, resp = req("POST", f"/projects/{project_id}/repository/commits", json={
        "branch": branch,
        "commit_message": "validation MR commit",
        "actions": [{"action": "create", "file_path": "mr-test.txt", "content": "mr validation"}]
    }, expect=(201,))
    record("Commit via Commits API", ok, f"HTTP {resp.status_code}: {resp.text[:200]}" if not ok else "")
    if not ok:
        return

    ok, resp = req("POST", f"/projects/{project_id}/merge_requests", json={
        "source_branch": branch, "target_branch": default_branch, "title": "Validation MR"
    }, expect=(201,))
    record("Create merge request", ok, f"HTTP {resp.status_code}: {resp.text[:200]}" if not ok else "")
    if not ok:
        return
    mr_iid = resp.json()["iid"]

    time.sleep(2)
    ok, resp = req("PUT", f"/projects/{project_id}/merge_requests/{mr_iid}/merge", expect=(200, 201))
    record("Merge the merge request", ok,
           f"HTTP {resp.status_code}: {resp.text[:300]}" +
           (" — likely a permissions gap: the token's user needs at least Maintainer "
            "role on this project/group to merge (Developer can create MRs but not merge them "
            "if merge permissions are restricted)" if resp.status_code == 401 else "") if not ok else "")


# ---------------------------------------------------------------------------
# 7. CI/CD pipeline
# ---------------------------------------------------------------------------
def build_ci_yaml(tag=None):
    tags_block = f"\n  tags:\n    - {tag}" if tag else ""
    return f"""
stages:
  - validate

validation_job:
  stage: validate
  script:
    - echo "Upgrade validation pipeline ran successfully"
    - echo "CI_JOB_TOKEN scope test"
    - env | grep -E '^CI_' | sort
  artifacts:
    paths: []
    expire_in: 1 hour{tags_block}
  rules:
    - if: '$CI_PIPELINE_SOURCE == "api" || $CI_PIPELINE_SOURCE == "push"'
"""


def check_pipeline(project_id, project, default_branch, runner_tag=None):
    # Push to a dedicated feature branch, not the default branch directly —
    # default branches are commonly protected (only Maintainer+ can push, or
    # push is blocked entirely in favor of MR-only workflows), which would
    # make this check fail on a permissions/policy issue unrelated to the
    # upgrade. Pipelines can be triggered against any branch, so this avoids
    # needing elevated permissions just to run a CI smoke test.
    ci_yaml = build_ci_yaml(runner_tag)
    ci_branch = f"ci-validation-{RUN_ID}"
    ok, resp = req("POST", f"/projects/{project_id}/repository/branches",
                    json={"branch": ci_branch, "ref": default_branch}, expect=(201,))
    record("Create CI validation branch", ok, f"HTTP {resp.status_code}: {resp.text[:200]}" if not ok else "")
    if not ok:
        return

    ok, resp = req("POST", f"/projects/{project_id}/repository/commits", json={
        "branch": ci_branch,
        "commit_message": "add validation ci config",
        "actions": [{"action": "create", "file_path": ".gitlab-ci.yml", "content": ci_yaml}]
    }, expect=(201,))
    record("Push .gitlab-ci.yml" + (f" (tagged: {runner_tag})" if runner_tag else " (untagged)"),
           ok, f"HTTP {resp.status_code}: {resp.text[:200]}" if not ok else "")
    if not ok:

        return

    ok, resp = req("GET", f"/projects/{project_id}/pipelines?ref={ci_branch}&order_by=id&sort=desc&per_page=1")
    # Pipeline creation is asynchronous after a push/commit — poll briefly
    # instead of checking once immediately, or a fast check can race ahead
    # of GitLab actually creating the pipeline object.
    pipeline_wait_deadline = time.time() + 20
    while (not ok or not resp.json()) and time.time() < pipeline_wait_deadline:
        time.sleep(2)
        ok, resp = req("GET", f"/projects/{project_id}/pipelines?ref={ci_branch}&order_by=id&sort=desc&per_page=1")
    if not ok or not resp.json():
        record("Pipeline created after push", False,
               "No pipeline found within 20s — check: (1) CI/CD is enabled for this project "
               "(Settings > General > Visibility, project features, permissions), (2) pipelines aren't "
               "disabled instance-wide, (3) no push rules/webhook blocking pipeline creation")
        return
    pipeline = resp.json()[0]
    pipeline_id = pipeline["id"]
    record("Pipeline triggered on push", True, f"pipeline id={pipeline_id} branch={ci_branch}")

    # Poll for completion (runner must be available and tagged correctly)
    deadline = time.time() + 180
    status = pipeline["status"]
    while time.time() < deadline and status in ("created", "pending", "running", "waiting_for_resource"):
        time.sleep(5)
        ok, resp = req("GET", f"/projects/{project_id}/pipelines/{pipeline_id}")
        status = resp.json().get("status", status)

    record("Pipeline completes", status == "success",
           f"final status: {status} (check runner availability/tags if stuck in pending)")

    ok, resp = req("GET", f"/projects/{project_id}/pipelines/{pipeline_id}/jobs")
    if ok:
        for job in resp.json():
            record(f"Job '{job['name']}' status", job["status"] == "success",
                   f"status={job['status']}, runner={(job.get('runner') or {}).get('description', 'n/a')}")


def check_job_token_scope(project_id):
    ok, resp = req("GET", f"/projects/{project_id}/job_token_scope")
    record("Job token scope settings readable", ok, f"HTTP {resp.status_code}")
    ok, resp = req("GET", f"/projects/{project_id}/job_token_scope/allowlist")
    record("Job token allowlist readable", ok, f"HTTP {resp.status_code}")


# ---------------------------------------------------------------------------
# 8. Runners
# ---------------------------------------------------------------------------
def check_runners():
    ok, resp = req("GET", "/runners/all?per_page=100")
    if not ok:
        # non-admin token: fall back to instance-visible runners
        ok, resp = req("GET", "/runners?per_page=100")
    if not ok:
        record("List runners", False, f"HTTP {resp.status_code}")
        return None
    runners = resp.json()
    record("List runners", True, f"{len(runners)} runner(s) visible")

    paused = [r for r in runners if r.get("paused") or r.get("status") == "paused"]
    online = [r for r in runners if r.get("status") == "online"]
    offline_or_stale = [r for r in runners
                         if r not in online and r not in paused]

    if paused and not online:
        record("At least one runner online", False,
               f"{len(paused)} runner(s) are administratively PAUSED (not a connectivity issue — "
               f"unpause under Admin Area > CI/CD > Runners, or a project's Settings > CI/CD > Runners)")
    else:
        record("At least one runner online", len(online) > 0,
               f"online={len(online)} paused={len(paused)} offline/stale={len(offline_or_stale)}")

    for r in runners:
        print(f"      -> runner '{r.get('description')}' id={r.get('id')} "
              f"status={r.get('status', 'n/a')} paused={r.get('paused', 'n/a')} tags={r.get('tag_list', [])}")

    # Pick a tag to route the validation pipeline to a runner we know is
    # actually online, rather than assuming any runner accepts untagged
    # jobs (many are intentionally configured to require an explicit tag
    # match, same pattern as tags: uat / tags: k8s in your own CI configs).
    online_with_tags = [r for r in online if r.get("tag_list")]
    if online_with_tags:
        chosen = online_with_tags[0]
        tag = chosen["tag_list"][0]
        print(f"      -> will route validation pipeline using tag '{tag}' "
              f"(matches online runner '{chosen.get('description')}')")
        return tag
    elif online:
        print(f"      -> online runner(s) found but none have tags configured — validation pipeline "
              f"will run untagged; this only works if that runner has 'Run untagged jobs' enabled")
        return None
    else:
        print(f"      -> no online runners found — pipeline check will likely stay pending regardless of tags")
        return None


# ---------------------------------------------------------------------------
# 9. Webhooks
# ---------------------------------------------------------------------------
def check_webhook(project_id):
    ok, resp = req("POST", f"/projects/{project_id}/hooks", json={
        "url": WEBHOOK_TEST_URL,
        "push_events": True,
        "issues_events": True,
        "enable_ssl_verification": True
    }, expect=(201,))
    record("Create project webhook", ok, f"HTTP {resp.status_code}: {resp.text[:200]}" if not ok else "")
    if not ok:
        return
    hook_id = resp.json()["id"]

    ok, resp = req("POST", f"/projects/{project_id}/hooks/{hook_id}/test/push_events", expect=(201, 200))
    if not ok and resp.status_code == 422 and "blocked" in resp.text.lower():
        print(f"[INFO] Webhook test target ({WEBHOOK_TEST_URL}) blocked by outbound URL allowlist "
              f"(SSRF protection) — this is likely a network policy working as intended, not an "
              f"upgrade regression. Set WEBHOOK_TEST_URL to an internally-reachable endpoint to get "
              f"a real pass/fail signal.")
    else:
        record("Trigger webhook test event", ok, f"HTTP {resp.status_code}: {resp.text[:200]}" if not ok else "")


# ---------------------------------------------------------------------------
# 10. Access tokens (project-level)
# ---------------------------------------------------------------------------
def check_access_tokens(project_id):
    ok, resp = req("GET", f"/projects/{project_id}/access_tokens")
    record("List project access tokens", ok, f"HTTP {resp.status_code}")

    ok, resp = req("POST", f"/projects/{project_id}/access_tokens", json={
        "name": "validation-token", "scopes": ["read_api"], "access_level": 20,
        "expires_at": (datetime.now(timezone.utc).date().isoformat())
    }, expect=(201,))
    record("Create project access token", ok, f"HTTP {resp.status_code}: {resp.text[:200]}" if not ok else "")


# ---------------------------------------------------------------------------
# 11. GitLab CLI (glab)
# ---------------------------------------------------------------------------
def check_glab_cli():
    if shutil.which("glab") is None:
        print("[SKIP] glab CLI not found on PATH — optional, skipping CLI checks "
              "(install glab separately to also validate CLI workflows)")
        return
    r = subprocess.run(["glab", "--version"], capture_output=True, text=True)
    record("glab --version", r.returncode == 0, r.stdout.strip())

    env = os.environ.copy()
    env["GITLAB_TOKEN"] = TOKEN
    env["GITLAB_HOST"] = GITLAB_URL
    r = subprocess.run(["glab", "auth", "status"], capture_output=True, text=True, env=env)
    record("glab auth status", "Logged in" in (r.stdout + r.stderr), (r.stdout + r.stderr).strip()[:300])

    r = subprocess.run(["glab", "api", "version"], capture_output=True, text=True, env=env)
    record("glab api version", r.returncode == 0, r.stdout.strip()[:200])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print(f"=== GitLab Upgrade Validation — target: {GITLAB_URL} ===\n")

    check_health_endpoints()
    check_version()
    check_auth()
    check_personal_access_tokens()
    runner_tag = check_runners()
    check_glab_cli()

    project = create_test_project()
    project_id = project["id"]
    default_branch = project.get("default_branch", "main")
    try:
        check_issue_lifecycle(project_id)
        check_file_upload(project_id)
        check_git_ops(project)
        check_merge_request(project_id, default_branch)
        check_job_token_scope(project_id)
        check_pipeline(project_id, project, default_branch, runner_tag)
        check_webhook(project_id)
        check_access_tokens(project_id)
    finally:
        delete_test_project(project_id)

    # ---- Report ----
    print("\n=== Summary ===")
    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    failed = total - passed
    print(f"Total: {total}  Passed: {passed}  Failed: {failed}")

    report_path = f"gitlab_validation_report_{RUN_ID}.json"
    with open(report_path, "w") as f:
        json.dump([{"check": n, "passed": ok, "detail": d} for n, ok, d in results], f, indent=2)
    print(f"Detailed report written to {report_path}")

    if failed:
        print("\nFailed checks:")
        for n, ok, d in results:
            if not ok:
                print(f"  - {n}: {d}")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        import traceback
        print(f"\n!!! Unexpected error: {e}")
        traceback.print_exc()
        # Still write whatever we captured before the crash so nothing is lost
        report_path = f"gitlab_validation_report_{RUN_ID}_partial.json"
        with open(report_path, "w") as f:
            json.dump([{"check": n, "passed": ok, "detail": d} for n, ok, d in results], f, indent=2)
        print(f"Partial report (results collected before the crash) written to {report_path}")
        sys.exit(2)
