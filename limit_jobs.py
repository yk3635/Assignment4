cat > /tmp/resubmit_stuck.py << 'EOF'
from prefect.client.orchestration import get_client
from prefect.client.schemas.filters import FlowRunFilter, FlowRunFilterState, FlowRunFilterStateType
from prefect.states import Scheduled
import asyncio, pendulum

DRY_RUN = False       # set True to just preview
TEST_LIMIT = 1        # set to None to process all matching runs

async def resubmit_pending_runs():
    async with get_client() as client:
        runs = await client.read_flow_runs(
            flow_run_filter=FlowRunFilter(
                state=FlowRunFilterState(type=FlowRunFilterStateType(any_=["PENDING"]))
            ),
            limit=200
        )
        print(f"Found {len(runs)} PENDING runs total")

        if TEST_LIMIT:
            runs = runs[:TEST_LIMIT]
            print(f"TEST_LIMIT set — only processing {len(runs)} run(s)\n")

        for r in runs:
            print(f"  {r.name:25s} {r.id}  scheduled={r.expected_start_time}")
            if not DRY_RUN:
                await client.set_flow_run_state(
                    flow_run_id=r.id,
                    state=Scheduled(scheduled_time=pendulum.now("utc"))
                )
        if not DRY_RUN:
            print(f"\nRescheduled {len(runs)} run(s) with scheduled_time=now")

asyncio.run(resubmit_pending_runs())
EOF
