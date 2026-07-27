# Anthropic NGINX — Operations Troubleshooting Runbook

**Project:** `anthropic-nginx` (AI endpoint proxy)
**Companion doc:** `anthropic-nginx-deployment-runbook.md` (deployment/CI issues live there — Section 9)
**Scope:** Runtime/operational issues with the container once deployed — not deployment
pipeline failures.
**Last updated:** 2026-07-23

---

## How to use this doc

Find the symptom closest to what you're seeing, confirm the diagnosis with the given
command(s), then apply the fix.

---

## 1. Container behavior issues

### 1.1 `Conflict. The container name "/anthropic-nginx" is already in use`

**Symptom:** Starting the container fails with a naming conflict.

**Cause:** `start-anthropic.sh` does a plain `docker run`, with no stop/remove step.
Running it against an already-running (or stopped-but-not-removed) container of the
same name always fails this way — it's not idempotent by design.

**Diagnosis:**
```bash
sudo docker ps -a | grep anthropic-nginx
```
Confirm whether a stopped (not removed) container is holding the name — `docker rm`
is required, not just `docker stop`, to free the name for a new `docker run`.

**Fix:**
```bash
sudo docker stop anthropic-nginx && sudo docker rm anthropic-nginx
sudo /opt/deploy/anthropic/scripts/start-anthropic.sh
```

---

### 1.2 Container running but serving stale config/image

**Symptom:** Behavior (headers, upstream, cert) looks like an old version, despite a
deploy having run.

**Diagnosis:**
```bash
sudo docker inspect anthropic-nginx --format '{{.Config.Image}}'
sudo docker inspect anthropic-nginx --format '{{.State.StartedAt}}'
diff /opt/deploy/anthropic/configs/nginx.conf <(sudo docker exec anthropic-nginx cat /etc/nginx/nginx.conf)
```

**Likely causes:**
1. Container wasn't restarted after new config/image was staged
2. Image tag wasn't actually changed, so the image-presence check skipped the pull
   (see 1.3 below)
3. Templated file diff shows a mismatch — wrong source template or stale variable

---

### 1.3 Image not being re-pulled despite expecting a refresh

**Symptom:** You expect a new image, but the container is still running the old one.

**Cause:** The deploy pipeline checks the target host for `<image>:<tag>` already
present, and skips the pull if a match is found — this is intentional idempotent
behavior, not a bug, but it means a *floating* tag (e.g. `stable-alpine`) that gets
rebuilt upstream with new content will not automatically trigger a fresh pull.

**Diagnosis:**
```bash
sudo docker image ls --format '{{.Repository}}:{{.Tag}}' | grep <anthropic_img>:<anthropic_version>
```
If this returns a match on the target host, that's why a pull was skipped.

**Fix (force a repull of the same tag):**
```bash
sudo docker rmi <anthropic_img>:<anthropic_version>
```
Then redeploy. Longer-term: prefer immutable, unique tags per build to avoid this
ambiguity entirely.

---

### 1.4 `nginx -t` fails inside the container

**Diagnosis:**
```bash
sudo docker exec anthropic-nginx nginx -t
```

**Common causes:**
- A bind-mounted file referenced in `nginx.conf` doesn't exist at the expected path
  on the host — check mounts vs. what `nginx.conf` expects:
  ```bash
  sudo docker inspect anthropic-nginx --format '{{json .Mounts}}' | python3 -m json.tool
  ```
- A templated variable rendered incorrectly (empty upstream, malformed directive) —
  inspect the rendered file directly:
  ```bash
  cat /opt/deploy/anthropic/configs/nginx.conf
  ```
- Cert/key/dhparam files missing or unreadable at the mounted paths:
  ```bash
  sudo docker exec anthropic-nginx ls -la /etc/nginx/certs/
  ```

---

### 1.5 Container up but endpoint not responding

**Diagnosis:**
```bash
sudo docker inspect anthropic-nginx --format '{{.State.Status}}'
sudo docker logs anthropic-nginx --tail 100
curl -sk https://localhost/<ai-endpoint-path> -o /dev/null -w "%{http_code}\n"
```

**Common causes:**
- Nginx running but proxy_pass/upstream misconfigured (check rendered `nginx.conf`)
- Cert mismatch or expired cert (check `openssl x509 -in <cert> -noout -dates`)
- `--network host` mode means the container binds directly to host ports — confirm
  nothing else on the host (another process, a leftover container) already holds
  the port:
  ```bash
  sudo ss -tlnp | grep :443
  ```

---

## 2. Logging issues

### 2.1 Single log file growing unbounded

**Symptom:** A single, ever-growing log file, not obviously tied to `/var/log/nginx`
paths you'd expect.

**Diagnosis — determine which case you're in:**
```bash
sudo docker exec anthropic-nginx ls -la /var/log/nginx/
```

- **If `access.log`/`error.log` are symlinks** (`-> /dev/stdout` / `-> /dev/stderr`):
  Docker's `json-file` log driver is capturing everything into
  `/var/lib/docker/containers/<id>/<id>-json.log` with no size cap by default.
  ```bash
  sudo docker inspect anthropic-nginx --format '{{.LogPath}}'
  ```
  This should not occur in the current setup (logs are bind-mounted to real files),
  but if it does, the container was likely started without the log mount — check
  `start-anthropic.sh` was actually used and the mount is present (Section 2.2 below).

- **If they're real files:** growth is from `logrotate` not running or misconfigured
  — see 2.3 and 2.4.

---

### 2.2 Confirming the log bind-mount is active

```bash
sudo docker inspect anthropic-nginx --format '{{json .Mounts}}' | python3 -m json.tool
```
Confirm an entry mounting `/data/anthropic_nginx/logs` (host) → `/var/log/nginx`
(container). If it's missing, the container was started from an outdated script
version — redeploy the current `start-anthropic.sh` and restart the container
(see deployment runbook).

---

### 2.3 Logs not rotating on schedule

**Diagnosis:**
```bash
cat /etc/logrotate.d/anthropic-nginx
sudo logrotate -d /etc/logrotate.d/anthropic-nginx   # dry run, verbose
ls -lrt /data/anthropic_nginx/logs/
```

**Common causes:**
- **Wrong path in the logrotate config** — confirm the glob
  (`/data/anthropic_nginx/logs/*.log`) actually matches where logs live. A stray or
  older config pointing at a different path (e.g. `/data/nginx/logs/*.log`) will
  silently rotate nothing at the path you expect while reporting success elsewhere.
- **Host cron/timer not running:**
  ```bash
  cat /etc/cron.daily/logrotate 2>/dev/null || sudo systemctl list-timers | grep logrotate
  ```
- **"Log does not need rotating" on manual test** — expected if logrotate's state
  file (`/var/lib/logrotate/logrotate.status`) shows it already rotated today.
  `daily` means "at most once per day," not "every time you run it." Force it:
  ```bash
  sudo logrotate -f /etc/logrotate.d/anthropic-nginx
  ```

---

### 2.4 Permission errors writing to mounted log directory after rotation

**Symptom:** nginx errors appear in `docker logs anthropic-nginx` after a rotation,
or the log file stops growing post-rotation.

**Cause:** `copytruncate` truncates the file in place, so the file keeps its
original ownership — but if the *directory* or newly created files end up with
different ownership (e.g. root vs. the nginx container's runtime UID), nginx may
lose write access.

**Diagnosis:**
```bash
sudo docker exec anthropic-nginx id nginx
ls -la /data/anthropic_nginx/logs/
```

**Fix:**
```bash
sudo chown -R <uid>:<gid> /data/anthropic_nginx/logs
```

---

### 2.5 Confirming rotation is truly working end-to-end

```bash
# Force a rotation
sudo logrotate -f /etc/logrotate.d/anthropic-nginx

# Confirm rotated + compressed files appear
ls -lrt /data/anthropic_nginx/logs/
# Expect: access.log, access.log.1.gz, error.log, error.log.1.gz, ...

# Confirm nginx is still writing to the active (post-truncate) file
sudo docker exec anthropic-nginx sh -c 'echo test >> /var/log/nginx/error.log'
tail -1 /data/anthropic_nginx/logs/error.log
```

---

## 3. General diagnostic commands (quick reference)

```bash
# Container state
sudo docker inspect anthropic-nginx --format '{{.State.Status}}'
sudo docker inspect anthropic-nginx --format '{{.State.StartedAt}}'
sudo docker inspect anthropic-nginx --format '{{.Config.Image}}'
sudo docker inspect anthropic-nginx --format '{{json .Mounts}}' | python3 -m json.tool
sudo docker inspect anthropic-nginx --format '{{json .HostConfig.LogConfig}}'

# Config validation
sudo docker exec anthropic-nginx nginx -t

# Recent errors
sudo docker logs anthropic-nginx --tail 100

# Port binding check (network host mode)
sudo ss -tlnp | grep :443

# Logrotate state/debug
cat /var/lib/logrotate/logrotate.status
sudo logrotate -d /etc/logrotate.d/anthropic-nginx
```

---

## 4. Escalation notes

If none of the above resolves the issue:

1. Capture `sudo docker logs anthropic-nginx --tail 200`
2. Capture `sudo docker inspect anthropic-nginx` (full, not just formatted fields)
3. Note the exact `anthropic_version` and target hostname involved
4. Check whether the issue correlates with a recent deploy — if so, cross-reference
   the deployment runbook's troubleshooting section (Section 9) as well

_Add team escalation contact / on-call channel here._
