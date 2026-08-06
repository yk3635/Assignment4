Here's the complete reclaim procedure — compaction first (logically frees old revisions), then defrag (physically shrinks the file on disk). Do this one member at a time.

Step 1: Get the current revision (do this once, any endpoint)
bash
etcdctl --endpoints=https://10.80.10.11:2379 \
  --cacert=/etc/ssl/etcd/ssl/ca.pem --cert=/etc/ssl/etcd/ssl/node-ahab1101.pem --key=/etc/ssl/etcd/ssl/node-ahab1101-key.pem \
  endpoint status --write-out=json | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['Status']['header']['revision'])"

This prints a number (e.g. 4421142605) — save it, you'll use it in Step 2. Since etcd replicates the revision counter across the cluster, one compact call is enough (it applies cluster-wide via raft), but defrag must be run against each member individually.

Step 2: Compact to that revision
bash
etcdctl --endpoints=https://10.80.10.11:2379 \
  --cacert=/etc/ssl/etcd/ssl/ca.pem --cert=/etc/ssl/etcd/ssl/node-ahab1101.pem --key=/etc/ssl/etcd/ssl/node-ahab1101-key.pem \
  compact 4421142605

(replace with the actual number from Step 1)

This tells etcd it can discard all revision history at or before that point — but it doesn't shrink the file yet, it just marks that space as reclaimable internally.

Step 3: Defrag each member, one at a time

Member .11:

bash
etcdctl --endpoints=https://10.80.10.11:2379 \
  --cacert=/etc/ssl/etcd/ssl/ca.pem --cert=/etc/ssl/etcd/ssl/node-ahab1101.pem --key=/etc/ssl/etcd/ssl/node-ahab1101-key.pem \
  defrag

Wait for it to complete and return success before moving on.

Member .13.75:

bash
etcdctl --endpoints=https://10.80.13.75:2379 \
  --cacert=/etc/ssl/etcd/ssl/ca.pem --cert=/etc/ssl/etcd/ssl/node-ahab1101.pem --key=/etc/ssl/etcd/ssl/node-ahab1101-key.pem \
  defrag

Member .139 (leader — do this last):

bash
etcdctl --endpoints=https://10.80.10.139:2379 \
  --cacert=/etc/ssl/etcd/ssl/ca.pem --cert=/etc/ssl/etcd/ssl/node-ahab1101.pem --key=/etc/ssl/etcd/ssl/node-ahab1101-key.pem \
  defrag
Step 4: Verify space was reclaimed
bash
etcdctl --endpoints=https://10.80.10.11:2379,https://10.80.13.75:2379,https://10.80.10.139:2379 \
  --cacert=/etc/ssl/etcd/ssl/ca.pem --cert=/etc/ssl/etcd/ssl/node-ahab1101.pem --key=/etc/ssl/etcd/ssl/node-ahab1101-key.pem \
  endpoint status --write-out=json | python3 -m json.tool

dbSize should now be close to dbSizeInUse (~0.86GB) on all three members, down from 1.6-1.7GB.

Important cautions before you run this
Defrag briefly blocks that member — it holds a lock on the backend during the operation, so requests routed to that specific member may see elevated latency for a few seconds to ~1 minute depending on DB size. With 3 members and only one being defragged at a time, the cluster overall stays available (the other 2 keep serving), but avoid running all three simultaneously.
Do this during a quieter window if possible, even though it's non-disruptive at the cluster level.
Order matters less here than for the quota restart (compact isn't a restart), but I'd still do the leader (.139) last just to minimize any chance of a leader-side latency blip affecting active writes.
This is safe and non-destructive — you're not deleting any live/current data, only physically compacting old MVCC history that's already logically gone.

Want this bundled with the quota-increase steps into one runbook doc (matching your other runbook formats) so you have a single reference for next time this happens?




Claude is AI an
