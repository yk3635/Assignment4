from prefect.client.orchestration import get_client
from prefect.client.schemas.filters import FlowRunFilter, FlowRunFilterState, FlowRunFilterStateType, FlowRunFilterId
from prefect.states import Scheduled
import asyncio, pendulum

DRY_RUN = True          # set False to actually restart
TEST_IDS = None         # e.g. ["019fd274-e296-7f27-9418-50dded06e1f9"] to restart just those
TEST_LIMIT = 20         # set to None to process all matching CRASHED runs

async def resubmit_crashed_runs():
    async with get_client() as client:
        if TEST_IDS:
            runs = await client.read_flow_runs(
                flow_run_filter=FlowRunFilter(
                    id=FlowRunFilterId(any_=TEST_IDS)
                ),
                limit=len(TEST_IDS)
            )
        else:
            runs = await client.read_flow_runs(
                flow_run_filter=FlowRunFilter(
                    state=FlowRunFilterState(type=FlowRunFilterStateType(any_=["CRASHED"]))
                ),
                limit=200
            )
        print(f"Found {len(runs)} CRASHED run(s) total")

        if TEST_LIMIT and not TEST_IDS:
            runs = runs[:TEST_LIMIT]
            print(f"TEST_LIMIT set — only processing {len(runs)} run(s)\n")

        for r in runs:
            print(f"  {r.name:25s} {r.id}  state={r.state_type}  ended={r.end_time}")
            if not DRY_RUN:
                await client.set_flow_run_state(
                    flow_run_id=r.id,
                    state=Scheduled(scheduled_time=pendulum.now("utc")),
                    force=True
                )
        if not DRY_RUN:
            print(f"\nRestarted {len(runs)} run(s) with scheduled_time=now")
        else:
            print(f"\nDRY_RUN is True — nothing was changed. Set DRY_RUN=False to actually restart.")

asyncio.run(resubmit_crashed_runs())
