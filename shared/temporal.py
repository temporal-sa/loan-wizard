# shared/temporal.py
"""Temporal connection settings shared by the Worker and the API client.

Consolidated here so the Task Queue name and the local-dev/Cloud connection
logic cannot drift between `worker.py` and `api/temporal_client.py`.

This module is imported only by the Worker and the API, never by Workflow code,
so importing the Temporal client bits here has no bearing on the Workflow sandbox.
"""
import os
from typing import TYPE_CHECKING, Any

from temporalio.common import SearchAttributeIndexedValueType
from temporalio.contrib.pydantic import pydantic_data_converter

from shared.search_attributes import ALL as SEARCH_ATTRIBUTES

if TYPE_CHECKING:
    from temporalio.client import Client

TASK_QUEUE = "loan-applications"

# Map the SDK's indexed-value-type enum to the operator-service proto enum used
# when registering an attribute. Only the types this app actually uses need an
# entry; an unmapped type raises in register_search_attributes() rather than
# registering the wrong type silently.
_PROTO_INDEXED_TYPE = {
    SearchAttributeIndexedValueType.KEYWORD: "INDEXED_VALUE_TYPE_KEYWORD",
    SearchAttributeIndexedValueType.TEXT: "INDEXED_VALUE_TYPE_TEXT",
    SearchAttributeIndexedValueType.INT: "INDEXED_VALUE_TYPE_INT",
    SearchAttributeIndexedValueType.DOUBLE: "INDEXED_VALUE_TYPE_DOUBLE",
    SearchAttributeIndexedValueType.BOOL: "INDEXED_VALUE_TYPE_BOOL",
    SearchAttributeIndexedValueType.DATETIME: "INDEXED_VALUE_TYPE_DATETIME",
    SearchAttributeIndexedValueType.KEYWORD_LIST: "INDEXED_VALUE_TYPE_KEYWORD_LIST",
}


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


async def register_search_attributes(client: "Client") -> list[str]:
    """Ensure this app's custom Search Attributes exist on the Cluster.

    Custom attributes must be registered before a Workflow can upsert them —
    upserting an unregistered attribute fails the Workflow Task and retries
    forever — so the Worker calls this at startup and the tests call it against
    their ephemeral servers. It is idempotent: existing attributes are left
    alone and only the missing ones are added, so it is safe to run on every
    boot. Returns the names that were newly registered (empty if all present).

    On a locked-down Cluster (e.g. Temporal Cloud) the account may lack
    permission to add attributes; in that case register them once out-of-band
    with `temporal operator search-attribute create` and this call is a no-op
    because they already exist.
    """
    from temporalio.api.enums.v1 import IndexedValueType
    from temporalio.api.operatorservice.v1 import (
        AddSearchAttributesRequest,
        ListSearchAttributesRequest,
    )
    from temporalio.service import RPCError, RPCStatusCode

    namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")

    # List first so we only add what's missing. The time-skipping test server
    # doesn't implement ListSearchAttributes; there, skip the diff and attempt to
    # add every attribute, tolerating an already-exists error below.
    try:
        existing = await client.operator_service.list_search_attributes(
            ListSearchAttributesRequest(namespace=namespace)
        )
        present = set(existing.custom_attributes.keys())
    except RPCError as e:
        if e.status != RPCStatusCode.UNIMPLEMENTED:
            raise
        present = set()

    to_add: dict[str, Any] = {}
    for key in SEARCH_ATTRIBUTES:
        if key.name in present:
            continue
        proto_name = _PROTO_INDEXED_TYPE.get(key.indexed_value_type)
        if proto_name is None:
            raise ValueError(
                f"Search attribute {key.name!r} has unsupported type "
                f"{key.indexed_value_type!r}; add it to _PROTO_INDEXED_TYPE."
            )
        to_add[key.name] = IndexedValueType.Value(proto_name)

    if not to_add:
        return []
    try:
        await client.operator_service.add_search_attributes(
            AddSearchAttributesRequest(namespace=namespace, search_attributes=to_add)
        )
    except RPCError as e:
        # A concurrent Worker (or a Cluster where listing wasn't possible) may
        # have registered them already — that's success, not an error.
        if e.status != RPCStatusCode.ALREADY_EXISTS:
            raise
        return []
    return list(to_add)
