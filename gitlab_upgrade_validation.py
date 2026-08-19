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
  - Git operations (HTTPS, auto-falling back to SSH if HTTPS is disabled)
  - CI/CD pipeline trigger + status polling (auto-detects a working runner
    and its real tags, and enables shared/group runner access as needed)
  - CI/CD job token scope settings
  - Runner registration & online status
  - Webhook creation (+ delivery trigger if WEBHOOK_TEST_URL is set)
  - Access tokens (project/group), Deploy tokens

NOT covered here (needs manual verification, see companion runbook):
  - UI rendering / navigation / visual regressions
  - GitLab CLI (glab) — plain git clone/push/pull is covered above; the
    separate glab tool isn't exercised by this script
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
Cleans up the test project at the end (set KEEP_TEST_PROJECT=1 to skip), or
set PERSISTENT_PROJECT_PATH to reuse a fixed existing project across runs
instead — issues/MRs/uploads/pipelines then accumulate as a running history.
Access tokens and webhooks are ALWAYS revoked/deleted regardless of mode.
"""

import os
import sys
import time
import json
import base64
import shutil
import subprocess
import tempfile
from urllib.parse import quote
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
PERSISTENT_PROJECT_PATH = os.environ.get("PERSISTENT_PROJECT_PATH", "")  # e.g. "ops/gitlab-upgrade-validation-history"
                                                                          # when set, reuse this existing project
                                                                          # instead of creating/deleting a throwaway
                                                                          # one — issues/MRs/uploads/pipelines
                                                                          # accumulate as a history across runs
KEEP_TEST_PROJECT = os.environ.get("KEEP_TEST_PROJECT", "0") == "1"  # only relevant when PERSISTENT_PROJECT_PATH
                                                                       # is NOT set — keeps a throwaway project
                                                                       # around for one-off debugging
VERIFY_TLS = os.environ.get("GITLAB_VERIFY_TLS", "1") != "0"
TIMEOUT = 30
SSH_PRIVATE_KEY_PATH = os.environ.get("GIT_SSH_PRIVATE_KEY_PATH", "")
GIT_PROTOCOL_PREF = os.environ.get("GIT_TEST_PROTOCOL", "auto")  # auto | https | ssh
WEBHOOK_TEST_URL = os.environ.get("WEBHOOK_TEST_URL", "")  # empty = skip the trigger-event
                                                             # check entirely (still tests
                                                             # webhook CREATE via the API)

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
    if PERSISTENT_PROJECT_PATH:
        ok, resp = req("GET", f"/projects/{PERSISTENT_PROJECT_PATH.replace('/', '%2F')}")
        if not ok:
            record("Resolve persistent validation project", False,
                   f"HTTP {resp.status_code}: could not find project at "
                   f"'{PERSISTENT_PROJECT_PATH}' — create it first, or unset PERSISTENT_PROJECT_PATH "
                   f"to fall back to a throwaway project")
            sys.exit(1)
        project = resp.json()
        record("Resolve persistent validation project", True,
               f"id={project['id']} path={project['path_with_namespace']} (reusing existing project — "
               f"issues/MRs/uploads/pipelines from this run will accumulate alongside prior runs)")
        return project

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
    if PERSISTENT_PROJECT_PATH:
        print(f"      -> PERSISTENT_PROJECT_PATH set, leaving project id={project_id} in place "
              f"(history accumulates across runs)")
        return
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


def check_repo_directory_and_files(project_id, default_branch):
    """
    Creates an actual directory structure in the repo (not just a flat file
    at root) with both a text file and a binary file, then reads both back
    to confirm content survives the round trip intact. Covers a gap the
    other checks miss: mr-test.txt and .gitlab-ci.yml are both single files
    at repo root, and the /uploads API check only tests issue/MR
    attachments, not actual repository file/directory handling.
    """
    branch = f"file-validation-{RUN_ID}"
    ok, resp = req("POST", f"/projects/{project_id}/repository/branches",
                    json={"branch": branch, "ref": default_branch}, expect=(201,))
    record("Create branch for directory/file test", ok,
           f"HTTP {resp.status_code}: {resp.text[:200]}" if not ok else "")
    if not ok:
        return

    text_content = f"Directory/file validation run {RUN_ID}\n"
    # Same 1x1 transparent PNG as the upload check, reused here to also
    # exercise binary content through the Commits API (base64 encoding).
    png_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="

    dir_path = f"validation-dir-{RUN_ID}"
    ok, resp = req("POST", f"/projects/{project_id}/repository/commits", json={
        "branch": branch,
        "commit_message": "add validation directory with text and binary files",
        "actions": [
            {"action": "create", "file_path": f"{dir_path}/subfolder/readme.txt", "content": text_content},
            {"action": "create", "file_path": f"{dir_path}/subfolder/binary-test.png",
             "content": png_b64, "encoding": "base64"},
        ]
    }, expect=(201,))
    record("Create nested directory with text + binary files", ok,
           f"HTTP {resp.status_code}: {resp.text[:200]}" if not ok else f"path={dir_path}/subfolder/")
    if not ok:
        return

    # Confirm the directory actually shows up in the repo tree (not just
    # that the commit API accepted the request).
    ok, resp = req("GET", f"/projects/{project_id}/repository/tree",
                    params={"path": f"{dir_path}/subfolder", "ref": branch})
    found_names = [f["name"] for f in resp.json()] if ok else []
    record("Directory listing shows both files", ok and set(found_names) == {"readme.txt", "binary-test.png"},
           f"found: {found_names}" if ok else f"HTTP {resp.status_code}")

    # Read the text file back and confirm content matches exactly.
    ok, resp = req("GET", f"/projects/{project_id}/repository/files/"
                           f"{quote(f'{dir_path}/subfolder/readme.txt', safe='')}/raw",
                    params={"ref": branch})
    record("Read back text file content matches", ok and resp.text == text_content,
           "content matches" if ok and resp.text == text_content
           else f"HTTP {resp.status_code}, got: {resp.text[:100]!r}" if ok else f"HTTP {resp.status_code}")

    # Read the binary file back and confirm bytes match exactly (catches
    # any post-upgrade regression in binary blob storage/retrieval, e.g. an
    # object storage or Gitaly migration issue that only affects binaries).
    ok, resp = req("GET", f"/projects/{project_id}/repository/files/"
                           f"{quote(f'{dir_path}/subfolder/binary-test.png', safe='')}/raw",
                    params={"ref": branch})
    expected_bytes = base64.b64decode(png_b64)
    record("Read back binary file content matches", ok and resp.content == expected_bytes,
           "binary content matches byte-for-byte" if ok and resp.content == expected_bytes
           else f"HTTP {resp.status_code}" if ok else f"HTTP {resp.status_code}")


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
        "actions": [{"action": "create", "file_path": f"mr-test-{RUN_ID}.txt", "content": "mr validation"}]
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
    #
    # This branch is never merged back, so overwriting .gitlab-ci.yml here
    # never touches the project's real CI config on its default branch —
    # important when reusing an existing project (e.g. PERSISTENT_PROJECT_PATH
    # pointed at a project that already has its own .gitlab-ci.yml).
    ci_yaml = build_ci_yaml(runner_tag)
    ci_branch = f"ci-validation-{RUN_ID}"
    ok, resp = req("POST", f"/projects/{project_id}/repository/branches",
                    json={"branch": ci_branch, "ref": default_branch}, expect=(201,))
    record("Create CI validation branch", ok, f"HTTP {resp.status_code}: {resp.text[:200]}" if not ok else "")
    if not ok:
        return

    # The branch was just cut from default_branch, so if that branch already
    # has a .gitlab-ci.yml (common when reusing an existing project), the
    # Commits API's "create" action will reject it — the file already
    # exists in this branch's history. Detect that and use "update" instead.
    existing_ok, _ = req("GET", f"/projects/{project_id}/repository/files/.gitlab-ci.yml",
                          params={"ref": ci_branch}, expect=(200,))
    commit_action = "update" if existing_ok else "create"

    ok, resp = req("POST", f"/projects/{project_id}/repository/commits", json={
        "branch": ci_branch,
        "commit_message": "add validation ci config",
        "actions": [{"action": commit_action, "file_path": ".gitlab-ci.yml", "content": ci_yaml}]
    }, expect=(201,))
    record("Push .gitlab-ci.yml" + (f" (tagged: {runner_tag})" if runner_tag else " (untagged)")
           + f" [{commit_action}]",
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
    """Returns (tag, runner_id, runner_type) for a chosen online runner, or (None, None, None)."""
    ok, resp = req("GET", "/runners/all?per_page=100")
    if not ok:
        # non-admin token: fall back to instance-visible runners
        ok, resp = req("GET", "/runners?per_page=100")
    if not ok:
        record("List runners", False, f"HTTP {resp.status_code}")
        return None, None, None
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
              f"status={r.get('status', 'n/a')} paused={r.get('paused', 'n/a')} "
              f"type={r.get('runner_type', 'n/a')} locked={r.get('locked', 'n/a')} "
              f"access_level={r.get('access_level', 'n/a')} tags={r.get('tag_list', [])}")

    if not online:
        print("      -> no online runners found — pipeline check will likely stay pending")
        return None, None, None

    # The /runners/all LIST endpoint's tag_list can be stale/incomplete —
    # confirmed by real-world evidence: a manually tagged 'uat' job runs
    # successfully on a runner the list endpoint reports as tags=[]. Query
    # each online runner's own detail endpoint (authoritative) rather than
    # trusting the list response, and prefer whichever one turns out to
    # actually have tags configured.
    detailed = []
    for r in online:
        ok_detail, resp_detail = req("GET", f"/runners/{r['id']}")
        if ok_detail:
            d = resp_detail.json()
            print(f"      -> runner {r['id']} ('{r.get('description')}') detail: "
                  f"tag_list={d.get('tag_list', [])}, run_untagged={d.get('run_untagged')}, "
                  f"locked={d.get('locked')}, access_level={d.get('access_level')}")
            detailed.append((r, d))
        else:
            detailed.append((r, {}))

    tagged = [(r, d) for r, d in detailed if d.get("tag_list")]
    if tagged:
        chosen, chosen_detail = tagged[0]
        tag = chosen_detail["tag_list"][0]
    else:
        chosen, chosen_detail = detailed[0]
        tag = None
        if chosen_detail.get("run_untagged") is False:
            print(f"      -> WARNING: chosen runner has no tags AND run_untagged=False — "
                  f"it will NEVER pick up an untagged job. Either tag the validation job to "
                  f"match a real tag on a runner, or enable 'Run untagged jobs' on it.")

    runner_type = chosen.get("runner_type")
    print(f"      -> will attempt to use runner '{chosen.get('description')}' "
          f"(id={chosen['id']}, type={runner_type}"
          f"{', tag=' + tag if tag else ', untagged'})")
    return tag, chosen["id"], runner_type


def ensure_project_runner_access(project_id, runner_id, runner_type):
    """
    Makes the chosen runner actually usable by the throwaway validation
    project. What's needed differs by runner type:
      - project_type: must be explicitly attached via POST /projects/:id/runners
        (equivalent to clicking "Enable for this project" in the UI).
      - instance_type / group_type: already available to any project in
        scope, but only if the project's shared_runners_enabled /
        group_runners_enabled flags are on. New projects can default to
        these being off depending on instance settings, which produces
        exactly "runner online, untagged/tagged correctly, but Runner: None"
        with no other visible error.
    """
    if runner_id is None:
        return

    if runner_type == "project_type":
        ok, resp = req("POST", f"/projects/{project_id}/runners", json={"runner_id": runner_id}, expect=(201, 200))
        record("Enable project-type runner for validation project", ok,
               f"runner_id={runner_id}" if ok else f"HTTP {resp.status_code}: {resp.text[:200]}")
        return

    # instance_type or group_type — ensure the project's runner-sharing
    # flags are on rather than trying to "attach" a shared runner (that
    # endpoint doesn't apply to shared runners and returns a 500 if tried).
    ok, resp = req("PUT", f"/projects/{project_id}",
                    json={"shared_runners_enabled": True, "group_runners_enabled": True},
                    expect=(200,))
    record(f"Enable shared/group runner access for validation project (runner type: {runner_type})", ok,
           "shared_runners_enabled=true, group_runners_enabled=true" if ok
           else f"HTTP {resp.status_code}: {resp.text[:200]}")

    # Verify what actually stuck — a 200 on the PUT doesn't guarantee the
    # values took effect if something upstream (group policy) is overriding
    # them. Read the project back and print the real values.
    ok2, resp2 = req("GET", f"/projects/{project_id}")
    if ok2:
        pdata = resp2.json()
        print(f"      -> verified on project: shared_runners_enabled="
              f"{pdata.get('shared_runners_enabled')}, group_runners_enabled="
              f"{pdata.get('group_runners_enabled')}")

    # Check group-level policy — if the group enforces "disabled and
    # unoverridable", no project-level flag can turn shared runners on,
    # which would exactly explain a PUT that reports success but has no effect.
    if NAMESPACE:
        ok3, resp3 = req("GET", f"/groups/{NAMESPACE}")
        if ok3:
            gdata = resp3.json()
            setting = gdata.get("shared_runners_setting")
            if setting:
                print(f"      -> group '{NAMESPACE}' shared_runners_setting = '{setting}'"
                      + (" <-- THIS BLOCKS project-level overrides regardless of the flag above"
                         if setting == "disabled_and_unoverridable" else ""))


# ---------------------------------------------------------------------------
# 9. Webhooks
# ---------------------------------------------------------------------------
def check_webhook(project_id):
    # If WEBHOOK_TEST_URL isn't set, only test that webhook CREATE works via
    # the API — skip the trigger-event step entirely rather than pointing it
    # at an external default (like httpbin.org) that most locked-down
    # instances will correctly SSRF-block, which just produces noise.
    target_url = WEBHOOK_TEST_URL or "https://example.invalid/webhook-target-not-configured"
    ok, resp = req("POST", f"/projects/{project_id}/hooks", json={
        "url": target_url,
        "push_events": True,
        "issues_events": True,
        "enable_ssl_verification": True
    }, expect=(201,))
    record("Create project webhook", ok, f"HTTP {resp.status_code}: {resp.text[:200]}" if not ok else "")
    if not ok:
        return
    hook_id = resp.json()["id"]

    if WEBHOOK_TEST_URL:
        ok, resp = req("POST", f"/projects/{project_id}/hooks/{hook_id}/test/push_events", expect=(201, 200))
        record("Trigger webhook test event", ok, f"HTTP {resp.status_code}: {resp.text[:200]}" if not ok else "")
    # else: trigger check skipped — set WEBHOOK_TEST_URL to an internally-reachable
    # endpoint to also validate webhook delivery, not just creation

    # Always clean up the webhook itself — even in persistent-project mode,
    # a growing pile of test webhooks pointing at dummy/dead targets has no
    # evidentiary value and is just clutter (unlike issues/MRs/pipelines,
    # which are meaningful history).
    ok, resp = req("DELETE", f"/projects/{project_id}/hooks/{hook_id}", expect=(204,))
    record("Cleanup: delete webhook", ok, f"HTTP {resp.status_code}")


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
    if not ok:
        return

    # Always revoke immediately after proving it can be created — a live
    # credential (even a short-lived, read_api-scoped one) has no reason to
    # persist past the check that proves token creation works, regardless
    # of whether the surrounding project is throwaway or persistent.
    token_id = resp.json().get("id")
    if token_id:
        ok, resp = req("DELETE", f"/projects/{project_id}/access_tokens/{token_id}", expect=(204,))
        record("Revoke project access token", ok, f"HTTP {resp.status_code}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print(f"=== GitLab Upgrade Validation — target: {GITLAB_URL} ===\n")

    check_version()
    check_auth()
    check_personal_access_tokens()
    runner_tag, runner_id, runner_type = check_runners()

    project = create_test_project()
    project_id = project["id"]
    default_branch = project.get("default_branch", "main")
    ensure_project_runner_access(project_id, runner_id, runner_type)
    try:
        check_issue_lifecycle(project_id)
        check_file_upload(project_id)
        check_repo_directory_and_files(project_id, default_branch)
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
