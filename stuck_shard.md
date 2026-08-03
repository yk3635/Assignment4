# Runbook: Stuck Shard Allocation (MANUAL_ALLOCATION / Retry Exhausted)

## When to use this runbook
Shards showing `UNASSIGNED` with allocation reason `MANUAL_ALLOCATION` or
`allocate.explanation` showing `failed_allocation_attempts >= index.allocation.max_retries`
(default max_retries = 5). This means ES gave up retrying on its own — it is
**not** actively trying to fix this, so it will stay stuck until acted on.

---

## Step 1 — Identify the stuck shards

```bash
curl -s "localhost:9200/_cat/shards" | grep UNASSIGNED
```

Note index name, shard number, and whether it's a primary (`p`) or replica (`r`).
**Do not proceed to Step 3 for primary shards without separate investigation —
this runbook is scoped to replica shard recovery only.**

---

## Step 2 — Confirm root cause before touching anything

Run allocation explain on each stuck shard:

```bash
curl -s -XGET "localhost:9200/_cluster/allocation/explain" -H 'Content-Type: application/json' -d '{
  "index": "<index_name>",
  "shard": <shard_number>,
  "primary": false
}' | jq '.unassigned_info, .node_allocation_decisions[]?.deciders[]? | select(.decision=="NO")'
```

**Check the `unassigned_info.reason` and decider output carefully:**

| Finding | Safe to retry? | Action |
|---|---|---|
| `ALLOCATION_FAILED` + high `failed_allocation_attempts`, no active `NO` deciders | ✅ Yes | Proceed to Step 3 |
| `disk_threshold` decider = `NO` | ❌ No | Free disk space / adjust watermark first |
| `same_shard` decider = `NO` | ❌ No | Needs explicit node targeting, not blanket retry |
| Allocation filtering / awareness rule = `NO` | ❌ No | Investigate index/cluster allocation settings first |

If you only see the first case (exhausted retries, no blocking decider), it's
safe to move to Step 3.

---

## Step 3 — Confirm this is a "no disruption" scenario

Before running the cluster-wide retry, verify:

- [ ] All stuck shards are **replicas**, not primaries (`p` column shows `r`)
- [ ] Corresponding primaries are `STARTED` and healthy
- [ ] Cluster health is not `red`
- [ ] No other unrelated `UNASSIGNED` shards exist that you haven't reviewed
      (since `retry_failed` is cluster-wide, it will retry *all* failed shards,
      not just the ones you're targeting)

```bash
curl -s "localhost:9200/_cluster/health?pretty"
```

If all boxes are checked, this is a safe, low-risk retry — no data loss
exposure since primaries remain untouched and serving.

---

## Step 4 — Trigger the retry

```bash
curl -s -XPOST "localhost:9200/_cluster/reroute?retry_failed=true" | jq .
```

This resets the internal failure counter cluster-wide and lets ES's normal
allocator re-attempt placement for every shard currently in a failed state.
It does not force placement onto any specific node — ES still applies all
normal allocation rules (disk watermarks, awareness, filtering).

---

## Step 5 — Verify resolution

```bash
curl -s "localhost:9200/_cat/shards" | grep UNASSIGNED
curl -s "localhost:9200/_cluster/health?pretty"
```

Confirm:
- Previously stuck shards now show `STARTED`
- Cluster health is `green` (or back to baseline)
- No new `UNASSIGNED` shards appeared as a side effect

---

## If retry_failed doesn't resolve it

Fall back to targeted, single-shard placement instead of the blanket retry:

```bash
curl -s -XPOST "localhost:9200/_cluster/reroute" -H 'Content-Type: application/json' -d '{
  "commands": [
    {
      "allocate_replica": {
        "index": "<index_name>",
        "shard": <shard_number>,
        "node": "<target_node_name>"
      }
    }
  ]
}'
```

Use this only after picking a target node with sufficient disk headroom and
confirming (via Step 2) it doesn't already hold the primary or another copy
of the same shard.

---

## Notes
- `retry_failed=true` is cluster-wide — it cannot be scoped to individual
  shards. If unrelated failed shards exist elsewhere for a different (bad)
  reason, this command will also retry those.
- This runbook covers **replica** shard recovery only. Stuck primary shards
  require separate investigation (potential data loss risk) and are out of
  scope here.
