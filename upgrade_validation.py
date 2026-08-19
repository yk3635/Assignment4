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
    for ep in ["/-/liveness", "/-/readiness", "/-/health"]:
        ok, resp = req("GET", f"{GITLAB_URL}{ep}", expect=(200,))
        record(f"Health endpoint {ep}", ok, f"HTTP {resp.status_code}: {resp.text[:200]}")


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
def check_git_https(project):
    clone_url = project["http_url_to_repo"]
    # inject token for auth: https://oauth2:<token>@host/path.git
    proto, rest = clone_url.split("://", 1)
    auth_url = f"{proto}://oauth2:{TOKEN}@{rest}"

    tmpdir = tempfile.mkdtemp(prefix="gl-validate-")
    try:
        r = subprocess.run(["git", "clone", "--quiet", auth_url, tmpdir],
                            capture_output=True, text=True, timeout=60)
        record("Git clone (HTTPS)", r.returncode == 0, r.stderr.strip()[:300])

        test_file = os.path.join(tmpdir, "validation-file.txt")
        with open(test_file, "w") as f:
            f.write(f"validation run {RUN_ID}\n")

        subprocess.run(["git", "-C", tmpdir, "config", "user.email", "validation@local"], check=True)
        subprocess.run(["git", "-C", tmpdir, "config", "user.name", "Upgrade Validation"], check=True)
        subprocess.run(["git", "-C", tmpdir, "add", "."], check=True)
        r = subprocess.run(["git", "-C", tmpdir, "commit", "-m", "validation commit"],
                            capture_output=True, text=True)
        record("Git commit", r.returncode == 0, r.stderr.strip()[:300])

        r = subprocess.run(["git", "-C", tmpdir, "push", "origin", "HEAD"],
                            capture_output=True, text=True, timeout=60)
        record("Git push (HTTPS)", r.returncode == 0, (r.stderr or r.stdout).strip()[:400])

        r = subprocess.run(["git", "-C", tmpdir, "pull", "--quiet"],
                            capture_output=True, text=True, timeout=60)
        record("Git pull (HTTPS)", r.returncode == 0, r.stderr.strip()[:300])
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 6. Merge request flow
# ---------------------------------------------------------------------------
def check_merge_request(project_id):
    branch = f"validation-branch-{RUN_ID}"
    ok, resp = req("POST", f"/projects/{project_id}/repository/branches",
                    json={"branch": branch, "ref": "main"}, expect=(201,))
    if not ok:
        # fall back to master if main doesn't exist
        ok, resp = req("POST", f"/projects/{project_id}/repository/branches",
                        json={"branch": branch, "ref": "master"}, expect=(201,))
    record("Create branch", ok, f"HTTP {resp.status_code}")
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
        "source_branch": branch, "target_branch": "main", "title": "Validation MR"
    }, expect=(201,))
    if not ok:
        ok, resp = req("POST", f"/projects/{project_id}/merge_requests", json={
            "source_branch": branch, "target_branch": "master", "title": "Validation MR"
        }, expect=(201,))
    record("Create merge request", ok, f"HTTP {resp.status_code}: {resp.text[:200]}" if not ok else "")
    if not ok:
        return
    mr_iid = resp.json()["iid"]

    time.sleep(2)
    ok, resp = req("PUT", f"/projects/{project_id}/merge_requests/{mr_iid}/merge", expect=(200, 201))
    record("Merge the merge request", ok, f"HTTP {resp.status_code}: {resp.text[:300]}" if not ok else "")


# ---------------------------------------------------------------------------
# 7. CI/CD pipeline
# ---------------------------------------------------------------------------
CI_YAML = """
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
    expire_in: 1 hour
  rules:
    - if: '$CI_PIPELINE_SOURCE == "api" || $CI_PIPELINE_SOURCE == "push"'
"""


def check_pipeline(project_id, project):
    ok, resp = req("POST", f"/projects/{project_id}/repository/commits", json={
        "branch": "main",
        "commit_message": "add validation ci config",
        "actions": [{"action": "create", "file_path": ".gitlab-ci.yml", "content": CI_YAML}]
    }, expect=(201,))
    if not ok:
        ok, resp = req("POST", f"/projects/{project_id}/repository/commits", json={
            "branch": "master",
            "commit_message": "add validation ci config",
            "actions": [{"action": "create", "file_path": ".gitlab-ci.yml", "content": CI_YAML}]
        }, expect=(201,))
    record("Push .gitlab-ci.yml", ok, f"HTTP {resp.status_code}: {resp.text[:200]}" if not ok else "")
    if not ok:
        return

    ok, resp = req("GET", f"/projects/{project_id}/pipelines?order_by=id&sort=desc&per_page=1")
    if not ok or not resp.json():
        record("Pipeline created after push", False, "No pipeline found — check runner/webhook config")
        return
    pipeline = resp.json()[0]
    pipeline_id = pipeline["id"]
    record("Pipeline triggered on push", True, f"pipeline id={pipeline_id}")

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
                   f"status={job['status']}, runner={job.get('runner', {}).get('description', 'n/a')}")


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
        return
    runners = resp.json()
    record("List runners", True, f"{len(runners)} runner(s) visible")
    online = [r for r in runners if r.get("status") == "online" or r.get("active")]
    offline = [r for r in runners if r not in online]
    record("At least one runner online", len(online) > 0,
           f"online={len(online)} offline/stale={len(offline)}")
    for r in runners:
        print(f"      -> runner '{r.get('description')}' id={r.get('id')} "
              f"status={r.get('status', 'n/a')} tags={r.get('tag_list', [])}")


# ---------------------------------------------------------------------------
# 9. Webhooks
# ---------------------------------------------------------------------------
def check_webhook(project_id):
    ok, resp = req("POST", f"/projects/{project_id}/hooks", json={
        "url": "https://httpbin.org/post",
        "push_events": True,
        "issues_events": True,
        "enable_ssl_verification": True
    }, expect=(201,))
    record("Create project webhook", ok, f"HTTP {resp.status_code}: {resp.text[:200]}" if not ok else "")
    if not ok:
        return
    hook_id = resp.json()["id"]

    ok, resp = req("POST", f"/projects/{project_id}/hooks/{hook_id}/test/push_events", expect=(201, 200))
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
        record("glab CLI installed", False, "glab not found on PATH — skip or install to test CLI workflows")
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
    check_runners()
    check_glab_cli()

    project = create_test_project()
    project_id = project["id"]
    try:
        check_issue_lifecycle(project_id)
        check_file_upload(project_id)
        check_git_https(project)
        check_merge_request(project_id)
        check_job_token_scope(project_id)
        check_pipeline(project_id, project)
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
    main()
