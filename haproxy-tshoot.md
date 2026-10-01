# HAProxy — On-Call Troubleshooting Runbook

**Scope:** server1 (DC1) and server2 (DC2), bare-metal RHEL 9.3, haproxy as systemd service.

**Config location:** `/etc/haproxy/haproxy.cfg` (+ any included files under `/etc/haproxy/conf.d/` if split)
**SSL cert location (RHEL default):** `/etc/pki/tls/certs/` and `/etc/pki/tls/private/` (or `/etc/haproxy/certs/` if using combined PEM files — check your `bind` line)
**Service manager:** `systemctl` (unit: `haproxy.service`)
**Stats/admin socket (if enabled):** typically `/run/haproxy/admin.sock` or `/var/lib/haproxy/stats`

---

## 1. First 60 Seconds — Triage

```bash
# 1. Is the process running?
systemctl status haproxy --no-pager -l

# 2. Is the config syntactically valid?
haproxy -c -f /etc/haproxy/haproxy.cfg

# 3. Is it listening on the expected frontend ports?
ss -tlnp | grep haproxy

# 4. Recent errors?
journalctl -u haproxy -n 50 --no-pager
tail -n 100 /var/log/haproxy.log 2>/dev/null || journalctl -u haproxy --since "10 min ago"

# 5. Are backends healthy from HAProxy's own point of view?
echo "show stat" | socat stdio /run/haproxy/admin.sock 2>/dev/null | cut -d',' -f1,2,18
# (field 18 = check status; look for UP/DOWN per server)
```

Note: HAProxy commonly logs to syslog/journal rather than a dedicated file unless you've configured `log` explicitly in the `global` section — check `grep -A2 "^global" /etc/haproxy/haproxy.cfg | grep log` if step 4 comes up empty.

---

## 2. Decision Tree

```
Is `systemctl status haproxy` NOT active?
 └─▶ Section 3 (Service Down)

Is haproxy active, but `haproxy -c` fails?
 └─▶ Section 4 (Config Error)

Is haproxy active + config valid, but clients get connection refused/reset?
 └─▶ Section 5 (Frontend/Bind Issue)

Is haproxy active + frontend reachable, but requests fail or 503?
 └─▶ Section 6 (Backend/Server Health Issue)

Is haproxy active + healthy, but SSL handshake fails or cert warning?
 └─▶ Section 7 (SSL/TLS Issue)

Is haproxy fine on this box, but the SITE overall is unreachable?
 └─▶ Section 8 (DNS / Wrong-DC Routing)

Is only ONE DC affected?
 └─▶ Section 9 (DC-Specific / Split-Brain Checks)

Are connections timing out under load, or seeing queueing/latency?
 └─▶ Section 10 (Capacity / Connection Limits)
```

---

## 3. Service Down

**Likely causes:** OOM kill, bad config on last start attempt, port conflict, ulimit exhaustion (haproxy is sensitive to `nofile`/max connections limits), disk full, SELinux denial.

```bash
systemctl status haproxy --no-pager -l
journalctl -u haproxy --since "30 min ago" --no-pager

# OOM check
dmesg -T | grep -i "out of memory\|killed process" | tail -20
journalctl -k --since "1 hour ago" | grep -i oom

# Disk space (haproxy will fail to start/log if root or /var is full)
df -h /var /etc

# Port already bound by something else?
ss -tlnp | grep -E ':80|:443'

# ulimit — a common silent killer for haproxy under load
systemctl show haproxy | grep -i limit
cat /etc/security/limits.d/*haproxy* 2>/dev/null
grep -i "ulimit-n\|maxconn" /etc/haproxy/haproxy.cfg

# SELinux denials — common after config/cert path changes
ausearch -m avc -ts recent 2>/dev/null | tail -30
sealert -a /var/log/audit/audit.log 2>/dev/null | tail -40
# (this exact failure mode — haproxy refusing to bind/load certs under
#  SELinux enforcing after an update — has been reported on RHEL-family 9.x)

# Restart and capture immediate result
systemctl restart haproxy
systemctl status haproxy --no-pager -l
```

**If it won't start due to config:** go to Section 4 before retrying restart.

---

## 4. Config Error (`haproxy -c` fails)

**Likely causes:** bad manual edit, duplicate `bind`, missing backend referenced by a `use_backend`/`default_backend`, ACL syntax error, missing cert file path.

```bash
# Always validate before touching the running service
haproxy -c -f /etc/haproxy/haproxy.cfg

# It reports the exact line, e.g.:
# [ALERT] config : parsing [/etc/haproxy/haproxy.cfg:47] : unknown keyword 'balance_algo'

# Recent changes
ls -lt /etc/haproxy/*.cfg /etc/haproxy/conf.d/ 2>/dev/null
cd /etc/haproxy && git log -5 --oneline 2>/dev/null
git diff HEAD~1 2>/dev/null

# Compare against the other DC as a sanity check
diff <(ssh server2-dc2 cat /etc/haproxy/haproxy.cfg) /etc/haproxy/haproxy.cfg
```

**Fix:**
```bash
# After correcting the syntax error and haproxy -c passes clean:
systemctl reload haproxy   # HAProxy reload is graceful — spawns new process,
                             # old one finishes in-flight connections then exits
```
Prefer `reload` over `restart` for config-only changes — HAProxy's reload model (unlike a hard restart) avoids dropping active connections.

---

## 5. Frontend/Bind Issue (connection refused/reset at the edge)

**Likely causes:** `bind` directive pointing at wrong IP/port, firewalld blocking the port, another process already holding the port, frontend defined but not referenced correctly.

```bash
# Confirm what haproxy thinks it's bound to
grep -A3 "^frontend" /etc/haproxy/haproxy.cfg

# Confirm it's actually listening
ss -tlnp | grep haproxy

# Local test bypassing DNS
curl -kv https://localhost/ -H "Host: server.example.com"

# Firewall check (RHEL 9 uses firewalld)
firewall-cmd --list-all
firewall-cmd --list-ports
```

**Fix:** correct the `bind` line or open the firewall port, then `nginx -c`-equivalent (`haproxy -c`) and `systemctl reload haproxy`.

---

## 6. Backend/Server Health Issue (503s, requests failing past the frontend)

**Likely causes:** backend app down, health check misconfigured/too strict, backend unreachable due to network/firewall, all servers in a backend marked DOWN.

```bash
# Live view of backend server health (requires stats socket enabled)
echo "show stat" | socat stdio /run/haproxy/admin.sock | cut -d',' -f1,2,18,36,37

# Or via the stats HTTP page if enabled:
curl -s http://localhost:<stats_port>/stats | grep -A2 "DOWN"

# Check what health check HAProxy is actually running
grep -A5 "^backend" /etc/haproxy/haproxy.cfg | grep -E "option httpchk|check"

# Test the backend directly, bypassing haproxy
curl -v http://<backend_ip>:<port>/<healthcheck_path>

# Check haproxy's own error log for specific server-down events
journalctl -u haproxy --since "1 hour ago" | grep -i "DOWN\|UP\|Server"

# Manually force a server back into rotation if it's flapping and you've confirmed it's healthy
echo "enable server <backend_name>/<server_name>" | socat stdio /run/haproxy/admin.sock
```

**If a backend is genuinely down:** this becomes that application's issue (Jenkins, GitLab, whatever's behind it) — hand off, but keep the `show stat` output and timestamps as evidence.

**If backend is up but HAProxy still marks it DOWN:** check the health check path/expected response matches what the app actually returns, and that `inter`/`fall`/`rise` timing isn't too aggressive for a slow-starting app.

---

## 7. SSL/TLS Issue

```bash
# Confirm cert path haproxy is actually loading (HAProxy wants a combined cert+key PEM)
grep "ssl crt" /etc/haproxy/haproxy.cfg

# Check expiry
openssl x509 -in /etc/haproxy/certs/server.example.com.pem -noout -dates -subject -issuer

# Verify cert+key pair actually match if they're separate files
openssl x509 -noout -modulus -in <cert.crt> | openssl md5
openssl rsa  -noout -modulus -in <key.key>  | openssl md5
# Must match

# Check permissions — haproxy user needs read access
ls -l /etc/haproxy/certs/

# Test live handshake
echo | openssl s_client -connect server.example.com:443 -servername server.example.com 2>/dev/null | openssl x509 -noout -dates
```

**Note:** unlike nginx (separate cert + key files), HAProxy's `bind ... ssl crt` typically expects a **single combined PEM** (cert + intermediate + key concatenated). A common failure after a cert renewal is dropping in a fresh cert without re-concatenating the key — check this first if TLS broke right after a renewal.

---

## 8. DNS / Wrong-DC Routing

```bash
dig +short server.example.com
curl -kv https://<DC1_IP>/ -H "Host: server.example.com"
curl -kv https://<DC2_IP>/ -H "Host: server.example.com"
dig +short server.example.com @8.8.8.8
dig +short server.example.com @1.1.1.1
```

If one DC's haproxy is healthy locally but DNS/GSLB isn't routing there, that's a DNS/GSLB health-probe issue — confirm haproxy is serving 200 on whatever path the probe actually hits (often distinct from your real health check path).

---

## 9. DC-Specific / Split-Brain Checks

```bash
diff <(ssh <other_dc_host> cat /etc/haproxy/haproxy.cfg) /etc/haproxy/haproxy.cfg
ssh <other_dc_host> haproxy -v
haproxy -v
diff <(ssh <other_dc_host> openssl x509 -in <cert> -noout -fingerprint) \
     <(openssl x509 -in <cert> -noout -fingerprint)
```

Config/version drift between DCs is itself often the root cause of "works in one DC, not the other" — flag it even if not the immediate trigger.

---

## 10. Capacity / Connection Limits

**Likely causes:** `maxconn` too low for current traffic, backend `maxconn` throttling and queueing requests, ulimit ceiling hit, SYN backlog exhausted under a traffic spike.

```bash
# Current connection counts vs configured limits
echo "show info" | socat stdio /run/haproxy/admin.sock | grep -E "CurrConns|MaxConn|Maxsock"

# Per-backend queueing (a growing Qcur means requests are backing up waiting for a server slot)
echo "show stat" | socat stdio /run/haproxy/admin.sock | cut -d',' -f1,2,3,4

# System-level connection tracking
ss -s
cat /proc/sys/net/core/somaxconn
```

**Fix:** raise `maxconn` in `global`/relevant `backend` sections if genuinely under-provisioned for current load — but treat this as a capacity-planning conversation, not just a config bump, if it's a recurring pattern rather than a one-off spike.

---

## 11. Escalation Criteria

- Backend application itself confirmed down → hand off to that app's owner.
- Both DCs simultaneously affected → treat as a wider incident, involve network/DNS team if GSLB is implicated.
- Cert issue requires reissuing from internal PKI you don't have access to.
- Capacity issue recurring → escalate as a sizing/capacity planning item, not a one-time fix.
- Root cause unclear after Sections 3–10 → capture `journalctl -u haproxy --since "2 hours ago"`, full `show stat` output, and `haproxy -c -f /etc/haproxy/haproxy.cfg` output, then escalate with those attached.

---

## 12. Quick Reference — Command Cheat Sheet

```bash
systemctl status haproxy --no-pager -l          # service state
systemctl reload haproxy                          # graceful reload (no dropped conns)
systemctl restart haproxy                          # hard restart
haproxy -c -f /etc/haproxy/haproxy.cfg             # validate config
journalctl -u haproxy -n 100 --no-pager            # recent service logs
ss -tlnp | grep haproxy                            # confirm listening ports
echo "show stat" | socat stdio /run/haproxy/admin.sock   # live backend health
echo "show info" | socat stdio /run/haproxy/admin.sock   # runtime stats/limits
curl -kv https://localhost/<path> -H "Host: server.example.com"   # local test bypassing DNS
openssl x509 -in <cert> -noout -dates              # cert expiry check
```

---

**Notes for future edits to this runbook:** add actual backend IPs/ports/health-check paths, stats socket path if non-default, GSLB/DNS provider name, and PKI contact, so on-call doesn't have to look them up mid-incident.
