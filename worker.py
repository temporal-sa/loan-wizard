# worker.py
import asyncio
import concurrent.futures
from temporalio.client import Client
from temporalio.worker import Worker

from workflows.loan_application import LoanApplicationWorkflow
from activities import loan_activities
from shared.temporal import TASK_QUEUE, address, connect_kwargs


async def main() -> None:
    client = await Client.connect(address(), **connect_kwargs())
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
