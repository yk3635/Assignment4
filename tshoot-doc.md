# Anthropic NGINX — On-Call Troubleshooting Runbook (BAU)

**Project:** `anthropic-nginx` (AI endpoint proxy)
**Companion doc:** `anthropic-nginx-deployment-runbook.md` — use that instead if the
issue started right after a deploy/config push, or involves Ansible/git.
**Audience:** On-call engineer responding to an alert or a "the AI endpoint is down/slow"
report.
**Last updated:** 2026-07-26

---

## 0. First 2 minutes — fast triage

Run this block first, every time. It tells you which of the four scenarios below
you're in.

```bash
# 1. Is the container even up?
sudo docker inspect anthropic-nginx --format 'Status={{.State.Status}} StartedAt={{.State.StartedAt}}'

# 2. Is nginx config valid right now?
sudo docker exec anthropic-nginx nginx -t

# 3. Is the endpoint reachable at all?
curl -sk -o /dev/null -w "HTTP %{http_code} | total %{time_total}s\n" \
  https://localhost:8783/anthropic/api/v1/messages?beta=true

# 4. Any errors in the last 5 minutes?
sudo docker logs anthropic-nginx --since 5m 2>&1 | grep -i error
```

| Result | Go to |
|---|---|
| Container not `running` / restarting in a loop | [Scenario A](#scenario-a-container-down-or-crash-looping) |
| Container running, but `curl` times out / connection refused | [Scenario B](#scenario-b-endpoint-unreachable-connection-refusedtimeout) |
| Container running, `curl` returns 5xx | [Scenario C](#scenario-c-5xx-errors) |
| Container running, `curl` succeeds but is slow, or errors mention resolver/buffering | [Scenario D](#scenario-d-slow-responses--intermittent-errors) |
| Everything above looks fine but a user still reports an issue | [Scenario E](#scenario-e-everything-looks-healthy-but-user-reports-an-issue) |

---

## Scenario A: Container down or crash-looping

```bash
sudo docker ps -a | grep anthropic-nginx
sudo docker logs anthropic-nginx --tail 100
```

**If `Status=exited`:**
- Check the exit reason and last log lines above — usually a config error on
  startup (`nginx -t`-equivalent failure baked into `docker run`) or a missing
  mounted file.
- Try to start it manually and watch the failure live:
  ```bash
  sudo docker start anthropic-nginx && sudo docker logs -f anthropic-nginx
  ```

**If `Status=running` but flapping (StartedAt keeps changing on repeat checks):**
- `--restart always` will keep relaunching a crashing container — check logs for
  the crash reason (segfault, OOM kill, config error) rather than assuming it'll
  self-heal:
  ```bash
  sudo docker inspect anthropic-nginx --format '{{.State.OOMKilled}}'
  dmesg -T | grep -i "out of memory" | tail -5
  ```

**If the container name is stuck in a bad state (won't start, "already in use", etc.):**
```bash
sudo docker stop anthropic-nginx 2>/dev/null
sudo docker rm anthropic-nginx 2>/dev/null
sudo /opt/deploy/anthropic/scripts/start-anthropic.sh
```

**Escalate if:** the container keeps crashing after a clean manual start with no
config changes made — this points to an image or environment problem beyond
config, not something to keep retrying solo.

---

## Scenario B: Endpoint unreachable (connection refused/timeout)

Container is running, but requests don't get a response.

```bash
# Is nginx actually bound to the port?
sudo ss -tlnp | grep :8783

# Anything else on the host holding the port (network host mode risk)?
sudo lsof -i :8783
```

**If nothing is listening on the port:**
- nginx likely failed to bind at startup — check `docker logs anthropic-nginx` for
  a bind error (`Address already in use`, or a startup config failure).
- Confirm no other process/container grabbed the port first — remember this
  container runs `--network host`, so it shares the host's port space directly.

**If something IS listening but connections still refuse/timeout:**
- Check host-level firewall/iptables rules haven't changed:
  ```bash
  sudo iptables -L -n | grep 8783
  ```
- Check from a *different* host/network segment, not just localhost — this
  isolates "nginx is broken" from "network path to this node is broken":
  ```bash
  curl -sk -o /dev/null -w "HTTP %{http_code}\n" https://<node-fqdn>:8783/...
  ```
  If localhost works but remote doesn't, this is a network/firewall/routing issue,
  not an nginx issue — escalate to network team.

**Escalate if:** port is bound, firewall looks clean, and it's still unreachable
from elsewhere — likely upstream network path issue outside this node.

---

## Scenario C: 5xx errors

```bash
sudo docker logs anthropic-nginx --tail 200 | grep -E "50[0-9]|error"
sudo docker exec anthropic-nginx nginx -t
```

**502 Bad Gateway** — nginx can't reach the upstream (the actual AI service behind
this proxy).
```bash
# Confirm what upstream nginx is configured to hit
sudo docker exec anthropic-nginx grep -A3 "proxy_pass\|upstream" /etc/nginx/nginx.conf

# Test reachability to that upstream directly from this host
curl -sk -o /dev/null -w "HTTP %{http_code}\n" <upstream-url>
```
If the upstream itself is down/unreachable, this is not an nginx-proxy problem —
escalate to whoever owns the upstream AI service.

**504 Gateway Timeout** — upstream is reachable but too slow to respond within
nginx's configured timeout.
```bash
sudo docker exec anthropic-nginx grep -i "proxy_read_timeout\|proxy_connect_timeout\|proxy_send_timeout" /etc/nginx/nginx.conf
```
Confirm whether the upstream is genuinely slow (its own problem) vs. the timeout
being too aggressive for legitimately long AI response times (large context /
long generations can take a while — this proxy may need a longer read timeout
than a typical web service).

**"send() failed (111: Connection refused) while resolving, resolver: ..."**
- This is a **DNS resolution failure inside nginx**, not an upstream outage.
- Confirm the configured `resolver` in `nginx.conf` is a real, working DNS server
  for this host — a mismatched resolver IP (e.g. `127.0.0.53` when this host
  doesn't run a local DNS stub) will cause every dynamic-hostname upstream lookup
  to fail instantly:
  ```bash
  sudo docker exec anthropic-nginx grep resolver /etc/nginx/nginx.conf
  cat /etc/resolv.conf   # compare against what nginx.conf has configured
  ```
- If they don't match, this is a config bug — fix in `nginx.conf.j2` per the
  deployment runbook and redeploy; it will not self-resolve by retrying.

**503 Service Unavailable**
- Check if this is nginx itself refusing (rate limiting / connection limits
  configured) vs. the upstream returning a 503 that's being passed through:
  ```bash
  sudo docker logs anthropic-nginx --tail 100 | grep 503
  ```

**Escalate if:** the upstream AI service itself is confirmed down/erroring — that's
outside this proxy's control, hand off to the upstream service owner with the
timestamp and error pattern.

---

## Scenario D: Slow responses / intermittent errors

**Check for request body buffering to disk (common on large AI payloads):**
```bash
sudo docker logs anthropic-nginx --since 30m 2>&1 | grep -c "buffered to a temporary file"
```
A high count on the `v1/messages` endpoint is expected behavior for large
request bodies, not necessarily a bug — but if it's happening on nearly every
request, disk I/O is adding latency. Check disk health on the temp path:
```bash
sudo docker exec anthropic-nginx df -h /var/cache/nginx/client_temp
```
If this is a chronic pattern (not a one-off during an incident), it's a tuning
item for the deployment runbook (`client_body_buffer_size`), not something to
fix live during an on-call incident — note it and move on unless disk is actually
full/slow.

**Check for intermittent DNS resolution flakiness (if upstream is a hostname, not IP):**
```bash
sudo docker logs anthropic-nginx --since 30m 2>&1 | grep -i "resolv"
```
Intermittent (not constant) resolver errors can indicate a flaky/overloaded DNS
server rather than a fully broken config — check DNS server health if this
correlates with wider DNS complaints elsewhere in the environment.

**Check host-level resource pressure:**
```bash
sudo docker stats anthropic-nginx --no-stream
top -bn1 | head -20
```
High CPU/memory on the container or host can manifest as slowness before it
manifests as outright errors.

**Escalate if:** resource pressure is confirmed and isn't something you can
relieve (e.g. host is genuinely undersized for current load) — this needs a
capacity conversation, not a live fix.

---

## Scenario E: Everything looks healthy, but user reports an issue

This is the most common false-alarm-vs-real-issue ambiguity. Narrow it down:

1. **Get the exact request** from the user — endpoint, method, timestamp, any
   error message/status code they saw client-side.
2. **Search logs for that exact window:**
   ```bash
   sudo docker exec anthropic-nginx grep "<timestamp-fragment>" /var/log/nginx/access.log
   sudo docker exec anthropic-nginx grep "<timestamp-fragment>" /var/log/nginx/error.log
   ```
3. **Reproduce with the same request shape** if possible (same endpoint, similar
   payload size) — a single large/malformed request can trigger an isolated
   413/400 without any broader outage.
4. **Check if the user is hitting this node specifically**, or if there's a
   GSLB/load-balancer routing them somewhere else intermittently — a resolver or
   DNS issue upstream of this node (client-side, not this proxy's own resolver)
   can look identical to a server-side problem from the user's perspective.

**Escalate if:** logs show nothing for the reported time window at all — this
usually means the request never reached this node, pointing to a routing/DNS
issue upstream of this proxy rather than anything on this host.

---

## Quick reference — commands used throughout

```bash
# Health snapshot
sudo docker inspect anthropic-nginx --format 'Status={{.State.Status}} StartedAt={{.State.StartedAt}}'
sudo docker exec anthropic-nginx nginx -t
sudo docker logs anthropic-nginx --tail 100

# Port / network
sudo ss -tlnp | grep :8783
sudo lsof -i :8783
sudo iptables -L -n | grep 8783

# Logs, scoped to a time window
sudo docker logs anthropic-nginx --since 30m
sudo docker exec anthropic-nginx grep "<pattern>" /var/log/nginx/access.log
sudo docker exec anthropic-nginx grep "<pattern>" /var/log/nginx/error.log

# Resource check
sudo docker stats anthropic-nginx --no-stream

# Functional test
curl -sk -o /dev/null -w "HTTP %{http_code} | total %{time_total}s\n" \
  https://localhost:8783/anthropic/api/v1/messages?beta=true
```

---

## When to hand off to the deployment runbook instead

If any of the following are true, stop here and use
`anthropic-nginx-deployment-runbook.md` instead:
- A deploy/config push happened shortly before the issue started
- You need to actually change `nginx.conf`, the start script, or any templated file
- The fix requires running the Ansible playbook
- The issue is a `git push` / GitLab merge problem, not a running-system problem

---

## Escalation

If triage above doesn't resolve it or points outside this node's control:

1. Capture the Section 0 fast-triage output in full
2. Capture `sudo docker logs anthropic-nginx --since 1h`
3. Note exact timestamps, endpoint(s), and client IP(s) involved
4. Note whether this correlates with a recent deploy (check with the deploy owner)

_Add team escalation contact / on-call channel here._
