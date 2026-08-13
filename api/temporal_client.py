# api/temporal_client.py
from temporalio.client import Client

# TASK_QUEUE is re-exported so existing imports (`api.main`, the tests) keep
# working; the connection settings live in one place in shared.temporal.
from shared.temporal import TASK_QUEUE, address, connect_kwargs

__all__ = ["TASK_QUEUE", "get_client", "wf_id"]

_client: Client | None = None


async def get_client() -> Client:
    global _client
    if _client is None:
        _client = await Client.connect(address(), **connect_kwargs())
    return _client


def wf_id(application_id: str) -> str:
    return f"loan-application-{application_id}"
