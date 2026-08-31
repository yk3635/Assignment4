# Nginx Reverse Proxy — On-Call Troubleshooting Runbook

**Scope:** server1 (DC1) and server2 (DC2), bare-metal RHEL 9.4, nginx as systemd service, reverse-proxying `server.example.com` to backend apps (e.g. `/jenkins`).

**Config location:** `/etc/nginx/nginx.conf` (+ any `/etc/nginx/conf.d/*.conf` includes)
**SSL cert location (RHEL default):** `/etc/pki/tls/certs/` (cert/chain) and `/etc/pki/tls/private/` (key)
**Service manager:** `systemctl` (unit: `nginx.service`)

---

## 1. First 60 Seconds — Triage

Run this block on the affected server(s) immediately. It tells you in under a minute whether this is a **service**, **config**, **backend**, **cert**, or **network/DNS** problem.

```bash
# 1. Is the process actually running?
systemctl status nginx --no-pager -l

# 2. Is the config syntactically valid?
nginx -t

# 3. Is it listening on the expected ports?
ss -tlnp | grep nginx

# 4. Recent errors?
journalctl -u nginx -n 50 --no-pager
tail -n 100 /var/log/nginx/error.log

# 5. Can you even resolve/reach the site locally?
curl -kv https://localhost/jenkins -H "Host: server.example.com"
```

Use the result of these 5 commands to jump directly to the relevant section below.

---

## 2. Decision Tree

```
Is `systemctl status nginx` NOT active?
 └─▶ Go to Section 3 (Service Down)

Is nginx active, but `nginx -t` fails?
 └─▶ Go to Section 4 (Config Error) — likely a bad reload/edit was pushed

Is nginx active + config valid, but users get 502/503/504?
 └─▶ Go to Section 5 (Upstream/Backend Issue — e.g. Jenkins down or unreachable)

Is nginx active + config valid, but SSL handshake fails or browser cert warning?
 └─▶ Go to Section 6 (SSL/TLS Issue)

Is nginx fine on this box, but users report the SITE is unreachable?
 └─▶ Go to Section 7 (DNS / Network / Wrong-DC Routing)

Is nginx fine on this box, but only ONE DC is affected?
 └─▶ Go to Section 8 (DC-Specific / Split-Brain Checks)
```

---

## 3. Service Down (`systemctl status` shows failed/inactive)

**Likely causes:** OOM-killed process, bad config on last start, port already in use, corrupted PID/lock file, disk full, SELinux denial.

```bash
# Check why it died
systemctl status nginx --no-pager -l
journalctl -u nginx --since "30 min ago" --no-pager

# Check for OOM kill
dmesg -T | grep -i "out of memory\|killed process" | tail -20
journalctl -k --since "1 hour ago" | grep -i oom

# Check disk space (nginx will refuse to start / log if root or /var is full)
df -h /var /etc

# Check if the port is already bound by something else
ss -tlnp | grep -E ':80|:443'

# Check SELinux denials (common cause of silent startup failure after a config change)
ausearch -m avc -ts recent 2>/dev/null | tail -30
sealert -a /var/log/audit/audit.log 2>/dev/null | tail -40

# Attempt restart, capture immediate failure reason
systemctl restart nginx
systemctl status nginx --no-pager -l
```

**If it won't start due to config:** go to Section 4 before retrying.

**If OOM-killed:** check for memory leaks/spikes from a bad upstream or worker_connections misconfig; check `free -h` and `top`/`htop` for current pressure before restarting, since restarting into the same memory pressure will just repeat the crash.

**Fix + restart:**
```bash
systemctl daemon-reload   # only if unit file itself was edited
systemctl restart nginx
systemctl enable nginx    # confirm it's still enabled for boot
```

---

## 4. Config Error (`nginx -t` fails)

**Likely causes:** bad edit to `nginx.conf` or a file under `conf.d/`, missing included file, duplicate `listen` directive, typo in directive/syntax, dangling symlink.

```bash
# Always run this before ANY reload — never `systemctl reload` blind
nginx -t

# It will point to the exact file + line number, e.g.:
# nginx: [emerg] unexpected "}" in /etc/nginx/conf.d/jenkins.conf:22

# Check recent config changes (if under git/config mgmt)
ls -lt /etc/nginx/conf.d/ /etc/nginx/*.conf
# If under version control:
cd /etc/nginx && git log -5 --oneline 2>/dev/null
git diff HEAD~1 2>/dev/null

# If no VCS, compare against the other DC's server as a sanity check
diff <(ssh server2-dc2 cat /etc/nginx/nginx.conf) /etc/nginx/nginx.conf
```

**Fix:**
- If a bad manual edit was made: correct the syntax error at the reported line, re-run `nginx -t` until it passes.
- If deployed via config management / GitOps, **roll back** rather than hand-editing:
  ```bash
  # example if config is git-managed and pulled via a deploy job — adjust to your actual pipeline
  cd /etc/nginx && git checkout HEAD~1 -- .
  nginx -t
  ```
- Only after `nginx -t` reports `syntax is ok` / `test is successful`:
  ```bash
  systemctl reload nginx   # graceful, no dropped connections
  ```
  Never `restart` for a config-only fix if `reload` will do — restart drops in-flight connections.

---

## 5. Upstream/Backend Issue (502/503/504 to `/jenkins`)

**Likely causes:** Jenkins process down, Jenkins listening on wrong port/interface, firewall blocking nginx→Jenkins hop, upstream timeout too short, backend overloaded.

```bash
# Confirm what nginx is actually proxying to for /jenkins
grep -A 10 "location /jenkins" /etc/nginx/conf.d/*.conf /etc/nginx/nginx.conf

# Test connectivity from the nginx box to the backend directly (bypass nginx)
curl -v http://<jenkins_backend_ip>:<port>/jenkins/login

# Is the backend port even open from this host?
nc -zv <jenkins_backend_ip> <port>

# Check nginx error log for the specific upstream error
tail -f /var/log/nginx/error.log
# Look for: "connect() failed", "upstream timed out", "no live upstreams"

# Check local firewall (if backend is remote) — RHEL 9 uses firewalld
firewall-cmd --list-all
```

**If backend is genuinely down:** this becomes a Jenkins issue, not nginx — escalate/hand off to whoever owns Jenkins, but keep nginx logs handy as evidence (timestamps, error strings).

**If backend is up but nginx still 502s:** check upstream timeout/buffer settings aren't too aggressive for a slow-starting Jenkins:
```bash
grep -E "proxy_connect_timeout|proxy_read_timeout|proxy_send_timeout" /etc/nginx/conf.d/*.conf
```

---

## 6. SSL/TLS Issue

**Likely causes:** expired cert, wrong cert/key pair, cert chain missing intermediate, permissions on private key, cert path mismatch after a rebuild.

```bash
# Confirm cert paths nginx is actually loading
grep -E "ssl_certificate|ssl_certificate_key" /etc/nginx/conf.d/*.conf /etc/nginx/nginx.conf

# Check expiry
openssl x509 -in /etc/pki/tls/certs/server.example.com.crt -noout -dates -subject -issuer

# Verify the cert and key actually match (compare modulus hashes)
openssl x509 -noout -modulus -in /etc/pki/tls/certs/server.example.com.crt | openssl md5
openssl rsa  -noout -modulus -in /etc/pki/tls/private/server.example.com.key | openssl md5
# These two hashes MUST match — if not, wrong key is paired with the cert

# Check file permissions (private key should be root-readable only, nginx user needs read)
ls -l /etc/pki/tls/private/server.example.com.key
# Should be owned appropriately (root:root or root:nginx, mode 600/640)

# Test the live handshake externally
curl -vI https://server.example.com/jenkins 2>&1 | grep -A5 "SSL certificate"
echo | openssl s_client -connect server.example.com:443 -servername server.example.com 2>/dev/null | openssl x509 -noout -dates
```

**Fix:**
- Expired cert → renew/reissue, replace file, `nginx -t`, then `systemctl reload nginx`.
- Mismatched key → confirm you're deploying the correct pair from your cert management source; don't hand-patch.
- Chain issue (browser says "not trusted" but expiry is fine) → confirm `ssl_certificate` points to the **full chain** file, not just the leaf cert.

---

## 7. DNS / Network / Wrong-DC Routing

**Likely causes:** `server.example.com` resolving to the wrong/unhealthy DC, DNS TTL/caching, GSLB/load-balancer health check flapping, one DC's public IP unreachable upstream of nginx entirely.

```bash
# What does DNS currently return? (run from outside both DCs if possible, e.g. your laptop)
dig +short server.example.com
nslookup server.example.com

# Confirm each DC's nginx is independently healthy regardless of DNS
curl -kv https://<DC1_IP>/jenkins -H "Host: server.example.com"
curl -kv https://<DC2_IP>/jenkins -H "Host: server.example.com"

# If using round-robin DNS or a GSLB, confirm which record(s) are currently being served
dig +short server.example.com @8.8.8.8
dig +short server.example.com @1.1.1.1
```

**If one DC's nginx is fine locally but DNS/GSLB isn't sending traffic there:** this is a DNS/GSLB health-check issue, not nginx itself — check whatever your DNS/GSLB health probe hits (often a `/healthz` or `/` endpoint) and confirm nginx is serving 200 on that specific path.

---

## 8. DC-Specific / Split-Brain Checks

When only one DC is affected, always diff against the healthy one before assuming it's environmental:

```bash
# Config parity check
diff <(ssh <other_dc_host> cat /etc/nginx/nginx.conf) /etc/nginx/nginx.conf
diff <(ssh <other_dc_host> ls /etc/nginx/conf.d/) <(ls /etc/nginx/conf.d/)

# Version parity check
ssh <other_dc_host> nginx -v
nginx -v

# Cert parity check (both DCs should be serving identical/valid certs unless intentionally split)
diff <(ssh <other_dc_host> openssl x509 -in /etc/pki/tls/certs/server.example.com.crt -noout -fingerprint) \
     <(openssl x509 -in /etc/pki/tls/certs/server.example.com.crt -noout -fingerprint)
```

A config or version drift between the two DCs is itself often the root cause — flag it even if it's not the immediate trigger, since it means the two "identical" servers aren't.

---

## 9. Escalation Criteria

Escalate beyond on-call nginx troubleshooting if:
- Backend (Jenkins) itself is confirmed down — hand off to Jenkins owner, this runbook's job is done once that's identified.
- Both DCs are simultaneously affected — treat as a wider incident, not a single-host issue; involve network/DNS team if GSLB is implicated.
- Cert issue requires reissuing from an internal CA/PKI team you don't have access to.
- Root cause is unclear after Sections 3–8 — capture `journalctl -u nginx --since "2 hours ago"`, `nginx -T` (full resolved config), and `/var/log/nginx/error.log` tail, then escalate with those attached.

---

## 10. Quick Reference — Command Cheat Sheet

```bash
systemctl status nginx --no-pager -l     # service state
systemctl reload nginx                    # graceful config reload (no dropped conns)
systemctl restart nginx                   # hard restart (drops in-flight conns)
nginx -t                                  # validate config syntax
nginx -T                                  # print full resolved config (all includes expanded)
journalctl -u nginx -n 100 --no-pager     # recent service logs
tail -f /var/log/nginx/error.log          # live error log
tail -f /var/log/nginx/access.log         # live access log
ss -tlnp | grep nginx                     # confirm listening ports
curl -kv https://localhost/<path> -H "Host: server.example.com"   # local test bypassing DNS
openssl x509 -in <cert> -noout -dates     # cert expiry check
```

---

**Notes for future edits to this runbook:** add specific backend IPs/ports, GSLB/DNS provider name, and cert renewal/PKI contact once confirmed, so on-call doesn't have to look them up mid-incident.
