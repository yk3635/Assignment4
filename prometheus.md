1. Get the exit code and termination reason (most important — do this first)
bash
kubectl get pod prometheus-server-6cf788db4b-hrmcg -n prometheus -o jsonpath='{.status.containerStatuses[0].lastState.terminated}' | jq

This tells you definitively why kubelet says it died:

reason: OOMKilled, exitCode: 137 → memory limit hit, kernel OOM-killed it
reason: Error, exitCode: 2 (or similar) → application-level crash/panic
reason: Completed / killed by liveness probe → look for exitCode: 137 too, but combined with liveness probe failures in events (SIGKILL after probe failures also shows as 137, so you have to correlate with events, not the code alone)

Also check current restart count and pattern:

bash
kubectl get pod prometheus-server-6cf788db4b-hrmcg -n prometheus -o jsonpath='{.status.containerStatuses[0].restartCount}'
2. Check if liveness probe (not just readiness) is killing it
bash
kubectl get pod prometheus-server-6cf788db4b-hrmcg -n prometheus -o yaml | grep -A10 -E "livenessProbe|readinessProbe"

This is the key question from our last message. If livenessProbe has a short initialDelaySeconds/timeoutSeconds/failureThreshold and points at /-/healthy or /-/ready, kubelet will SIGKILL and restart the container while it's still mid-WAL-replay — because liveness failures cause a restart, not just a "not receiving traffic" state like readiness does. This is the single most common cause of Prometheus restart loops during slow replay.

Compare failureThreshold × periodSeconds against your observed replay time (~11 min from your first log). If the math doesn't give the container 11+ minutes before liveness gives up, that's your answer.

3. Rule out OOM explicitly
bash
kubectl describe pod prometheus-server-6cf788db4b-hrmcg -n prometheus | grep -A5 "Last State"
kubectl top pod prometheus-server-6cf788db4b-hrmcg -n prometheus
kubectl get pod prometheus-server-6cf788db4b-hrmcg -n prometheus -o jsonpath='{.spec.containers[0].resources}'

Compare requested/limit memory against actual usage during startup — TSDB replay is memory-hungry (loads chunks into memory), so if limits.memory is tight, OOM during replay is very plausible and would look similar to a liveness kill in symptoms but very different in root cause/fix.

4. Check node-level events (in case it's not the pod's fault at all)
bash
kubectl get events -n prometheus --sort-by='.lastTimestamp' | grep -i prometheus
kubectl describe node <node-where-pod-runs> | grep -A10 Conditions

Rule out: node memory pressure, node NotReady causing eviction, node.kubernetes.io/not-ready:NoExecute toleration kicking in (I see you have a 300s toleration for that — worth checking if the node briefly flapped NotReady).

5. Check why replay takes so long (explains the whole window of vulnerability)
bash
kubectl exec -it prometheus-server-6cf788db4b-hrmcg -n prometheus -- du -sh /data
kubectl exec -it prometheus-server-6cf788db4b-hrmcg -n prometheus -- ls -la /data/wal

A large WAL (from an earlier unclean shutdown that skipped a checkpoint) makes every subsequent restart slower, compounding the probe-timeout problem. If WAL is unusually large, that's a symptom of a prior bad restart, not the current one.

Priority order

Step 1 and 2 will almost certainly give you the answer. My strong suspicion, given lastState had Ready: False and hundreds of 503/timeout probe failures with no OOM or crash message anywhere in logs, is: liveness probe killing the container before TSDB replay finishes, creating a self-perpetuating loop (kill → restart → replay resets → kill again).

Run step 1 and 2 and paste the output — that'll confirm it in one shot.

Diagnosed memory issue and charted diagnostic pathway forward
Diagnosed memory issue and charted diagnostic pathway forward

That settles it definitively — it's OOMKilled, not a probe-driven kill.

json
"exitCode": 137,
"reason": "OOMKilled",
"startedAt": "2026-08-03T21:09:41Z",
"finishedAt": "2026-08-03T21:26:00Z"

So the container ran for about 16 minutes before the kernel OOM-killer hit it, matching your earlier ~11-minute WAL replay window plus a few minutes of actual serving. This changes the diagnosis — the probe failures you saw (context deadline exceeded, 503) weren't the cause of the restart, they were a symptom of the same underlying problem: Prometheus was under memory pressure the whole time, which made it slow to respond to probes right up until it got killed.

Why OOM makes sense here, mechanically:

WAL/chunk snapshot replay loads a lot of data into memory before TSDB is ready to serve.
Your earlier log line, msg="updated GOGC" old=100 new=75, is Prometheus's automatic memory-limit-aware GC tuning — it only kicks in when Prometheus detects a container memory limit via cgroups and lowers GOGC to garbage-collect more aggressively to try to stay under it. That's actually a strong hint the memory limit is already tight relative to what the process needs.
If replay + normal working-set memory exceeds the container's limits.memory, the kernel cgroup OOM-killer fires — independent of any Kubernetes probe logic.
Next steps to confirm and fix

1. Get current memory request/limit vs actual usage:

bash
kubectl get pod prometheus-server-6cf788db4b-hrmcg -n prometheus -o jsonpath='{.spec.containers[0].resources}'
kubectl top pod prometheus-server-6cf788db4b-hrmcg -n prometheus

2. Check WAL/data size — bigger WAL means bigger replay memory spike:

bash
kubectl exec -it prometheus-server-6cf788db4b-hrmcg -n prometheus -- du -sh /data
kubectl exec -it prometheus-server-6cf788db4b-hrmcg -n prometheus -- du -sh /data/wal

3. Check how many series/targets it's tracking (high cardinality = high memory):

bash
kubectl exec -it prometheus-server-6cf788db4b-hrmcg -n prometheus -- wget -qO- http://localhost:9090/api/v1/status/tsdb

Look at numSeries and numLabelPairs in the response.

4. Check node memory pressure at the OOM timestamp (was the node itself tight on memory, causing the kernel to reap this pod first?):

bash
kubectl describe node <node-name> | grep -A5 "Allocated resources"
Likely fix path

Given the pattern (long replay + GOGC auto-tuning down to 75 + OOM after ~16 min), this looks like a memory limit that's undersized for your actual series/cardinality, not a fluke. The fix is almost certainly to raise resources.limits.memory on the prometheus-server container — possibly significantly, since replay memory usage can spike well above steady-state serving memory.

Want to pull the current resource limits and TSDB series count so we can size an appropriate increase?



# 1. Is it still in replay, or has it actually stabilized as unready?
kubectl logs prometheus-server-6cf788db4b-cl2hv -n prometheus | grep -i "replay\|ready to receive"

# 2. Current memory usage on the new node - is it climbing toward another OOM?
kubectl top pod prometheus-server-6cf788db4b-cl2hv -n prometheus

# 3. Has it actually restarted yet, or is this still the original container?
kubectl get pod prometheus-server-6cf788db4b-cl2hv -n prometheus -o jsonpath='{.status.containerStatuses[0].restartCount}'

# 4. Node memory pressure on ahab1205 - the real test of your hypothesis
kubectl describe node ahab1205 | grep -A5 "Allocated resources"
kubectl top node ahab1205

Step 1 — Confirm current state
bash
kubectl get pod -n prometheus -l app=prometheus-server -o wide
kubectl get pod -n prometheus -l app=prometheus-server -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}'

Note the pod name and restart count — you'll compare against this after the fix.

Step 2 — Check the actual data-loss window (so you know what you're accepting)
bash
POD=<current-pod-name>
kubectl exec -it $POD -n prometheus -- sh -c 'ls -la /bitnami/prometheus/data/wal | head -5; ls -la /bitnami/prometheus/data/wal | tail -5'
kubectl exec -it $POD -n prometheus -- sh -c 'ls -lt /bitnami/prometheus/data | grep -v wal | head -5'

This tells you the WAL segment date range and the timestamp of the most recent successfully persisted block — that gap is what you'll lose. Sanity-check it's acceptable before continuing.

Step 3 — Scale Prometheus down to zero

Stops the crash loop and lets you safely touch the PVC without a live process fighting you.

bash
# Confirm whether it's a Deployment or StatefulSet first:
kubectl get deploy,sts -n prometheus | grep prometheus

# Then scale down (use whichever applies):
kubectl scale deployment prometheus-server -n prometheus --replicas=0
# or
kubectl scale statefulset prometheus-server -n prometheus --replicas=0

Wait for the pod to fully terminate:

bash
kubectl get pod -n prometheus -l app=prometheus-server -w
Step 4 — Launch a temporary debug pod with the same PVC mounted

Don't try to exec into a terminated pod — spin up a throwaway pod that mounts the same prometheus-server PVC so you can clear the WAL directly.

yaml
# debug-pvc-pod.yaml
apiVersion: v1
kind: Pod
metadata:
  name: prometheus-wal-cleanup
  namespace: prometheus
spec:
  containers:
  - name: cleanup
    image: busybox
    command: ["sleep", "3600"]
    volumeMounts:
    - name: data
      mountPath: /data
  volumes:
  - name: data
    persistentVolumeClaim:
      claimName: prometheus-server
bash
kubectl apply -f debug-pvc-pod.yaml
kubectl wait --for=condition=Ready pod/prometheus-wal-cleanup -n prometheus --timeout=60s
Step 5 — Back up the WAL first (cheap insurance, skip only if disk space is tight)
bash
kubectl exec -it prometheus-wal-cleanup -n prometheus -- sh -c 'du -sh /data/wal'
# If you have space to spare on the volume or want extra safety, tar it before deleting:
kubectl exec -it prometheus-wal-cleanup -n prometheus -- sh -c 'tar -czf /data/wal-backup-$(date +%s).tar.gz -C /data wal 2>/dev/null || echo "skipping backup, low space"'
Step 6 — Clear the WAL and the lockfile
bash
kubectl exec -it prometheus-wal-cleanup -n prometheus -- sh -c 'rm -rf /data/wal/*'
kubectl exec -it prometheus-wal-cleanup -n prometheus -- sh -c 'rm -f /data/lock'
kubectl exec -it prometheus-wal-cleanup -n prometheus -- sh -c 'ls -la /data'

Confirm wal/ is now empty and the persisted block directories (the 01K... ULID-named dirs) are still present and untouched.

Step 7 — Tear down the debug pod
bash
kubectl delete pod prometheus-wal-cleanup -n prometheus
Step 8 — (Recommended) Raise the memory limit before bringing Prometheus back

Give it headroom so a future replay/compaction doesn't OOM again:

bash
kubectl set resources deployment prometheus-server -n prometheus --limits=memory=120Gi -c prometheus
# or for statefulset:
kubectl set resources statefulset prometheus-server -n prometheus --limits=memory=120Gi -c prometheus

(Adjust the number based on your node's actual free memory from kubectl top node ahab1205 — don't set it above what the node can realistically give it.)

Step 9 — Scale back up
bash
kubectl scale deployment prometheus-server -n prometheus --replicas=1
# or
kubectl scale statefulset prometheus-server -n prometheus --replicas=1
Step 10 — Watch startup closely
bash
kubectl get pod -n prometheus -l app=prometheus-server -w

In another terminal, tail logs:

bash
kubectl logs -f -n prometheus -l app=prometheus-server

With WAL cleared, startup should now skip the long replay entirely and reach "Server is ready to receive web requests." within seconds/a couple minutes, not 11+ minutes.

Step 11 — Verify it's actually healthy
bash
kubectl get pod -n prometheus -l app=prometheus-server
kubectl top pod -n prometheus -l app=prometheus-server
kubectl exec -it <new-pod> -n prometheus -- wget -qO- http://localhost:9090/-/ready

Should return Prometheus Server is Ready.

Step 12 — Confirm no immediate restart, then move to root-cause hardening

Once stable for 15–20 minutes with restartCount still at 0, you're past the crisis. After that, worth circling back to:

Why did the WAL grow to 371GB in the first place (likely the crash loop itself, but worth confirming compaction interval settings)
Whether the memory limit needs to stay permanently higher or if there's a cardinality/retention issue driving baseline usage up
Checking kubectl top node ahab1205 periodically to make sure the new limit doesn't create node-level pressure for other pods
