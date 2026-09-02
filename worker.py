# worker.py
import asyncio
import concurrent.futures
from temporalio.client import Client
from temporalio.worker import Worker

from workflows.loan_application import LoanApplicationWorkflow
from activities import loan_activities
from shared.temporal import (
    TASK_QUEUE,
    address,
    connect_kwargs,
    register_search_attributes,
)


async def main() -> None:
    client = await Client.connect(address(), **connect_kwargs())
    # Custom Search Attributes must exist before the Workflow upserts them, or
    # every Task fails and retries forever. Registering here (idempotent) means a
    # fresh local dev server just works; on a locked-down Cluster the attributes
    # are pre-registered out-of-band and this is a no-op.
    registered = await register_search_attributes(client)
    if registered:
        print(f"Registered Search Attributes: {', '.join(registered)}")
    with concurrent.futures.ThreadPoolExecutor(max_workers=100) as executor:
        worker = Worker(
            client,
            task_queue=TASK_QUEUE,
            workflows=[LoanApplicationWorkflow],
            activities=loan_activities.ALL,
            activity_executor=executor,
        )
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
