python3 -c "
from prefect.client.orchestration import get_client
from prefect.states import Scheduled
import asyncio, pendulum

FLOW_RUN_ID = '019fb345-b016-7a09-b5e8-c4314048436d'  # <-- paste the ID you want to test

async def main():
    async with get_client() as client:
        await client.set_flow_run_state(
            flow_run_id=FLOW_RUN_ID,
            state=Scheduled(scheduled_time=pendulum.now('utc'))
        )
        print(f'Rescheduled {FLOW_RUN_ID}')

asyncio.run(main())
"
