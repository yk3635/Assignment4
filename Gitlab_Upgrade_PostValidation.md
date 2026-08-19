# GitLab Post-Upgrade Validation Runbook

**Scope:** Validate GitLab CE after 18.11.11 → 19.2.4 upgrade — CI/CD, tokens,
pipelines, runners, UI, glab CLI, git operations, issues/uploads.

**Companion script:** `gitlab_upgrade_validation.py` (automates everything that
can be automated via API/git/CLI). This document covers running that script,
interpreting failures, plus the manual UI checklist that can't be scripted.

---

## 0. Prerequisites

```bash
pip install requests --break-system-packages
# optional, for CLI checks:
# https://gitlab.com/gitlab-org/cli — install glab if not already present
```

Create a **dedicated PAT** for this (don't reuse a personal one) — an admin
service account token is ideal so runner-listing and job-token-scope checks
don't get blocked by permissions:

- GitLab UI → Admin Area → Users → (service account) → Impersonation Tokens,
  or Preferences → Access Tokens
- Scopes: `api`, `read_api`, `write_repository`

```bash
export GITLAB_URL="https://your-gitlab-instance"
export GITLAB_TOKEN="glpat-xxxxxxxxxxxxxxxxxxxx"
export GITLAB_NAMESPACE="your-test-group"     # a group you can freely create/delete projects in
export EXPECTED_VERSION="19.2.4"
```

---

## 1. Run the automated suite

```bash
python3 gitlab_upgrade_validation.py
```

It will:
1. Hit `/-/liveness`, `/-/readiness`, `/-/health`, `/api/v4/version`
2. Authenticate with the PAT, list active PATs
3. List runners and confirm at least one is online
4. Check `glab` CLI auth + basic commands (if installed)
5. Create a throwaway test project, then exercise:
   - Issue create → comment → close
   - File/attachment upload API
   - Git clone / commit / push / pull over HTTPS
   - Branch → commit via Commits API → MR → merge
   - Job token scope + allowlist endpoints
   - Push a `.gitlab-ci.yml`, confirm a pipeline triggers, poll until it
     finishes, check each job's runner and status
   - Webhook create + test-trigger
   - Project access token create/list
6. Deletes the test project (unless `KEEP_TEST_PROJECT=1`)
7. Writes `gitlab_validation_report_<timestamp>.json` and exits non-zero on
   any failure

Run it against **staging/lab first**, then again against **prod** right after
the maintenance window closes.

### Reading failures

| Failure | Likely cause | Where to look |
|---|---|---|
| Health endpoints fail | Puma/Workhorse not fully up, or reconfigure still running | `gitlab-ctl tail`, container logs |
| Pipeline stuck in `pending` | No runner with matching tags, or runner can't reach new instance (TLS/DNS) | Runner logs (`journalctl -u gitlab-runner` or container logs), runner `config.toml` URL/token still valid post-upgrade |
| Pipeline created but job `failed` | Runner executor issue, or new GitLab CI/CD syntax behavior change | Job log via API/UI |
| Job token scope endpoints 403/404 | Permission/feature-flag issue, or you're using a non-admin token | Re-run with admin-scoped PAT |
| Git push over HTTPS fails | HAProxy/ingress routing changed post-upgrade, or Workhorse LFS/git config drifted | `curl -v` against the same URL, check HAProxy backend health |
| Webhook test fails | Outbound network policy (`deny_all_requests_except_allowed`) blocking httpbin.org — expected in locked-down envs | Point the webhook at an internal reachable target instead |
| glab auth status fails | `glab` version too old for new instance API surface, or `GITLAB_HOST`/token mismatch | `glab --version`, update if very old |

---

## 2. Runner-specific checks (beyond what the script covers)

The script confirms runners are **online and pick up jobs**, but also
manually verify per runner host:

```bash
# On each runner host
gitlab-runner --version
gitlab-runner verify                 # confirms registration against new GitLab version
gitlab-runner list
```

- [ ] Runner version is still compatible with GitLab 19.2 (GitLab Runner
      supports N-2/N+2 minor version skew; if your runners are old, plan a
      runner upgrade too — mismatched major versions can silently drop features)
- [ ] Confirm TLS trust — if you did any custom CA cert imports for the old
      GitLab endpoint, re-verify runners still trust the cert chain post-upgrade
- [ ] Confirm runner tags still match what your `.gitlab-ci.yml` files expect
      (tags are sometimes reset if you re-registered runners during upgrade)
- [ ] If using Kubernetes executor: confirm the executor's service account /
      RBAC still has needed permissions in your cluster
- [ ] Check concurrent job limits (`concurrent =` in `config.toml`) are still
      being honored — pull a real multi-job pipeline through and watch for queuing

---

## 3. Token validation checklist (beyond script coverage)

- [ ] **Legacy PATs** — existing tokens created pre-upgrade still authenticate
      (script checks this implicitly by using one)
- [ ] **Fine-grained PATs** (beta since 18.11, GA-track in 19.x) — create one
      scoped to a single project, confirm it can/can't access things outside scope
- [ ] **Deploy tokens** — pull a private repo/registry image using an existing
      deploy token, confirm it still works
- [ ] **Group access tokens** — same CRUD test as project access tokens, at
      group level
- [ ] **CI_JOB_TOKEN cross-project access** — if you use the allowlist feature,
      confirm cross-project job token pushes/pulls between two real projects
      still respect the allowlist (this changed behavior in 18.0 — worth a
      double-check post-major-upgrade)
- [ ] **OAuth applications** — if you have any GitLab OAuth apps (e.g. Grafana,
      internal tools authenticating via GitLab OAuth), do a live login test
- [ ] **SSH keys / git-over-SSH** — the script only tests HTTPS; manually run:
  ```bash
  git clone git@your-gitlab-instance:group/project.git
  ```
      Confirm `sshd`/`gitlab-shell` didn't regress (this is a common upgrade
      casualty if `gitlab-shell` version drifted)

---

## 4. Manual UI checklist

Walk through as a normal user, then again as an admin. Budget ~30–45 min.

### Navigation & core UI
- [ ] Login page loads, login succeeds (password + any SSO/LDAP if configured)
- [ ] Left sidebar renders correctly, no broken icons/links
- [ ] Personal homepage (new since 18.5) loads with to-dos/MRs/issues
- [ ] Global search and project-scoped search return results
- [ ] Admin Area loads — Users, Settings, Monitoring, Background Migrations
      pages all render (check Background Migrations shows all `finished`)

### Projects & repository
- [ ] Create a project from UI, from a template
- [ ] Browse file tree, view file contents, view raw file
- [ ] Blob viewer + inline blame (new in 19.1) renders correctly
- [ ] Commit list loads, filtering works (redesigned in 19.1)
- [ ] Create/edit a file via Web IDE
- [ ] Wiki: create page, edit, sidebar toggle (repositioned in 18.11), sticky
      action bar, emoji reactions (new in 19.1)

### Issues / work items
- [ ] Create issue, add labels, assignees, milestone
- [ ] Comment, edit comment, upload an image/attachment via drag-and-drop in
      the comment box (separate code path from the API upload the script tests)
- [ ] Close/reopen issue
- [ ] Work Items list view (consolidated Issues/Epics since 18.10) loads
- [ ] If you use Epics: epic weights (new in 18.11) display and rollup correctly

### Merge requests
- [ ] Create MR from a branch with actual diff content
- [ ] Rapid Diffs view (beta since 19.0) — toggle it and confirm diffs render
- [ ] Leave inline comments, resolve threads
- [ ] Approve, merge
- [ ] Reports tab (new in 19.0) shows security/quality findings if scanners ran
- [ ] Stacked MRs indicator (new in 19.1) — if you use stacked MRs, confirm
      the stack navigator shows correctly

### CI/CD (UI side, complements the scripted pipeline test)
- [ ] Pipelines list loads, pipeline detail/graph view renders
- [ ] Job log streams live (not just static after completion)
- [ ] Manually trigger a pipeline from UI with custom variables
- [ ] Retry a failed job, cancel a running job
- [ ] CI/CD → Runners settings page shows correct runner list/status
- [ ] Schedules page — confirm existing pipeline schedules survived the
      upgrade and their next-run time is correct
- [ ] Environments / deployments page loads if you use them
- [ ] Reconfigure inputs on a manual MR pipeline run (new in 18.11) — try
      overriding an input value when triggering

### Registries & packages
- [ ] Container Registry — push/pull an image, browse tags in UI
- [ ] Package Registry — publish/view a package (npm, Maven, etc. — whatever
      you actually use)
- [ ] If you use Terraform modules: package protection rules (new in 18.11)
      still enforce correctly

### Admin-specific
- [ ] Background Migrations page — all `finished`/`finalized`
- [ ] System hooks / webhooks still fire (cross-check with script's webhook test)
- [ ] Check `gitlab-rake gitlab:check` output is clean:
  ```bash
  sudo gitlab-rake gitlab:check SANITIZE=true
  ```
- [ ] Check Sidekiq queue isn't backed up post-upgrade:
  ```bash
  sudo gitlab-rake gitlab:sidekiq:info  # or check /admin/background_jobs in UI
  ```

---

## 5. GitLab CLI (glab) manual spot-check

Beyond what the script automates:

```bash
glab auth login --hostname your-gitlab-instance   # interactive, confirms browser/token flow
glab repo clone group/project
glab mr list
glab mr create --title "test" --description "test"
glab ci status
glab ci view
glab issue list
```

- [ ] Confirm output formatting isn't broken (API response shape changes
      between major versions occasionally break older `glab` parsers — if
      anything looks garbled, check your `glab` version against GitLab 19.2's
      minimum supported CLI version)

---

## 6. Automating this in CI (optional next step)

Once the script is proven out manually, wrap it in a scheduled pipeline so
every future patch upgrade (19.2.x → 19.3.x, etc.) gets the same smoke test
automatically:

```yaml
# .gitlab-ci.yml on an internal "ops-validation" project
post_upgrade_validation:
  stage: validate
  image: python:3.12-slim
  before_script:
    - pip install requests --break-system-packages
  script:
    - python3 gitlab_upgrade_validation.py
  rules:
    - if: '$CI_PIPELINE_SOURCE == "schedule" || $CI_PIPELINE_SOURCE == "web"'
  variables:
    GITLAB_URL: "https://your-gitlab-instance"
    GITLAB_NAMESPACE: "ops-validation-sandbox"
  # GITLAB_TOKEN should come from a masked/protected CI/CD variable, not hardcoded
```

Run this **on a runner that is NOT hosted by the GitLab instance itself**
where possible (or at least confirm the runner survives the instance being
briefly unavailable during upgrade), so a broken GitLab instance doesn't also
break your ability to validate it.

---

## 7. Rollback trigger criteria

Define upfront what failure threshold triggers a rollback decision rather
than a fix-forward decision, e.g.:

- Any **git push/pull over HTTPS or SSH** failure → rollback candidate
  (core function broken for all users)
- **All runners offline/unable to pick up jobs** → rollback candidate
- **Background migrations not completing within N hours** → escalate, don't
  necessarily rollback (see earlier `web_hook_logs_daily` discussion)
- Isolated **UI cosmetic issues** or a single **beta feature** misbehaving →
  fix-forward, not rollback
