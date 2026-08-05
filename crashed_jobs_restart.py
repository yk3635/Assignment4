#!/usr/bin/env python3
"""
restart_crashed_flows.py

Restarts CRASHED Prefect flow runs by resetting their state to Scheduled,
so the work pool re-submits the underlying job.

Modes:
  --test <flow_run_id>          Restart exactly one flow run (sanity check)
  --batch-file <path> --limit N Restart up to N flow runs from a file of IDs
  --dry-run                     (default) Show what WOULD happen, no changes
  --execute                     Actually perform the state change

Input file format (one flow run ID per line, '#' comments allowed):
    019fd2ec-9ae3-7121-be2c-08137be0f587
    019fd2ee-a5f9-7751-b8e3-3931758ea530
    ...

Usage examples:
  # 1) Dry-run a single flow run first
  python restart_crashed_flows.py --test 019fd2ec-9ae3-7121-be2c-08137be0f587

  # 2) Actually restart that one flow run
  python restart_crashed_flows.py --test 019fd2ec-9ae3-7121-be2c-08137be0f587 --execute

  # 3) Dry-run the next batch of 20 from a file
  python restart_crashed_flows.py --batch-file crashed_ids.txt --limit 20

  # 4) Execute that batch for real
  python restart_crashed_flows.py --batch-file crashed_ids.txt --limit 20 --execute

  # Optionally auto-pull all CRASHED runs from Prefect instead of a file:
  python restart_crashed_flows.py --from-crashed --limit 20 --flow-name-contains opencti_workflow_updater_long
"""

import argparse
import asyncio
import sys
from datetime import timedelta

from prefect.client.orchestration import get_client
from prefect.client.schemas.filters import (
    FlowRunFilter,
    FlowRunFilterState,
    FlowRunFilterStateType,
    FlowRunFilterFlowName,
)
from prefect.client.schemas.objects import StateType
from prefect.states import Scheduled


async def fetch_crashed_ids(client, limit, flow_name_contains=None):
    """Pull CRASHED flow run IDs directly from Prefect, most recent first."""
    flow_run_filter = FlowRunFilter(
        state=FlowRunFilterState(
            type=FlowRunFilterStateType(any_=[StateType.CRASHED])
        ),
    )
    if flow_name_contains:
        flow_run_filter.flow_name = FlowRunFilterFlowName(like_=flow_name_contains)

    runs = await client.read_flow_runs(
        flow_run_filter=flow_run_filter,
        limit=limit,
        sort="START_TIME_DESC",
    )
    return [str(r.id) for r in runs]


def load_ids_from_file(path):
    ids = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            ids.append(line)
    return ids


async def restart_flow_run(client, flow_run_id, dry_run=True):
    try:
        run = await client.read_flow_run(flow_run_id)
    except Exception as e:
        return flow_run_id, "ERROR", f"could not read flow run: {e}"

    if run.state_type != StateType.CRASHED:
        return flow_run_id, "SKIPPED", f"current state is {run.state_type}, not CRASHED"

    if dry_run:
        return flow_run_id, "DRY-RUN-OK", f"flow={run.flow_id} name={run.name} would set state -> Scheduled"

    try:
        await client.set_flow_run_state(
            flow_run_id=flow_run_id,
            state=Scheduled(scheduled_time=None),
            force=True,
        )
        return flow_run_id, "RESTARTED", f"name={run.name}"
    except Exception as e:
        return flow_run_id, "ERROR", str(e)


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--test", metavar="FLOW_RUN_ID", help="Restart a single flow run by ID")
    group.add_argument("--batch-file", metavar="PATH", help="File containing flow run IDs, one per line")
    group.add_argument("--from-crashed", action="store_true", help="Pull CRASHED runs directly from Prefect")

    parser.add_argument("--limit", type=int, default=20, help="Max number of runs to process in batch mode (default 20)")
    parser.add_argument("--flow-name-contains", default=None, help="Optional filter when using --from-crashed")
    parser.add_argument("--execute", action="store_true", help="Actually perform the restart (default is dry-run)")

    args = parser.parse_args()
    dry_run = not args.execute

    if args.limit > 20:
        print(f"WARNING: limit {args.limit} exceeds recommended batch size of 20. Capping at 20.")
        args.limit = 20

    async with get_client() as client:
        if args.test:
            ids = [args.test]
        elif args.batch_file:
            ids = load_ids_from_file(args.batch_file)[: args.limit]
        else:
            ids = await fetch_crashed_ids(client, args.limit, args.flow_name_contains)

        if not ids:
            print("No flow run IDs to process.")
            return

        mode = "DRY RUN" if dry_run else "EXECUTE"
        print(f"=== {mode}: processing {len(ids)} flow run(s) ===\n")

        results = []
        for fr_id in ids:
            fr_id, status, detail = await restart_flow_run(client, fr_id, dry_run=dry_run)
            results.append((fr_id, status, detail))
            print(f"{status:15s} {fr_id}  {detail}")

        print("\n=== Summary ===")
        counts = {}
        for _, status, _ in results:
            counts[status] = counts.get(status, 0) + 1
        for status, count in counts.items():
            print(f"  {status}: {count}")

        if dry_run:
            print("\nThis was a dry run. Re-run with --execute to actually restart these flow runs.")


if __name__ == "__main__":
    asyncio.run(main())
