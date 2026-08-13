# shared/temporal.py
"""Temporal connection settings shared by the Worker and the API client.

Consolidated here so the Task Queue name and the local-dev/Cloud connection
logic cannot drift between `worker.py` and `api/temporal_client.py`.

This module is imported only by the Worker and the API, never by Workflow code,
so importing the Temporal client bits here has no bearing on the Workflow sandbox.
"""
import os
from typing import Any

from temporalio.contrib.pydantic import pydantic_data_converter

TASK_QUEUE = "loan-applications"


def address() -> str:
    """The Temporal Cluster address — the local dev server unless overridden."""
    return os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")


def connect_kwargs() -> dict[str, Any]:
    """Environment-driven `Client.connect` kwargs shared by local dev and Cloud.

    Local dev needs nothing. For Cloud, set TEMPORAL_ADDRESS and
    TEMPORAL_NAMESPACE and provide TEMPORAL_API_KEY (which implies TLS); a bare
    TEMPORAL_TLS=true also enables TLS for a self-hosted cluster behind it.
    """
    kwargs: dict[str, Any] = {
        "namespace": os.environ.get("TEMPORAL_NAMESPACE", "default"),
        "data_converter": pydantic_data_converter,
    }
    api_key = os.environ.get("TEMPORAL_API_KEY")
    if api_key:
        kwargs["api_key"] = api_key
        kwargs["tls"] = True
    elif os.environ.get("TEMPORAL_TLS", "").lower() in {"1", "true", "yes"}:
        kwargs["tls"] = True
    return kwargs
