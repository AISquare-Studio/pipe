"""Composio meta-connector for aisquare.pipe — 500+ SaaS toolkits through one connector."""

from aisquare_pipe_composio.client import (
    TriggerCursor,
    is_transient_error,
    load_trigger_cursor,
    save_trigger_cursor,
)
from aisquare_pipe_composio.connections import (
    ConnectionRequest,
    connection_status,
    initiate_connection,
    list_connections,
    wait_for_active,
)
from aisquare_pipe_composio.connector import ComposioSink, ComposioSource
from aisquare_pipe_composio.constants import (
    MAX_EVENT_CLOCK_SKEW_MS,
    MAX_TRIGGER_PAGES_PER_POLL,
    SEEN_IDS_MAX,
)
from aisquare_pipe_composio.factory import composio_sink, composio_source
from aisquare_pipe_composio.triggers import ComposioTriggersSource

# The trigger cursor's host contract (a host that keeps the triggers source's
# position itself: pipe-as-service's poller). See TriggerCursor; pinned by
# tests/test_trigger_cursor_contract.py.
TRIGGER_CURSOR_API = (
    "MAX_EVENT_CLOCK_SKEW_MS",
    "MAX_TRIGGER_PAGES_PER_POLL",
    "SEEN_IDS_MAX",
    "TriggerCursor",
    "is_transient_error",
    "load_trigger_cursor",
    "save_trigger_cursor",
)

__all__ = [
    "MAX_EVENT_CLOCK_SKEW_MS",
    "MAX_TRIGGER_PAGES_PER_POLL",
    "SEEN_IDS_MAX",
    "TRIGGER_CURSOR_API",
    "ComposioSink",
    "ComposioSource",
    "ComposioTriggersSource",
    "ConnectionRequest",
    "TriggerCursor",
    "composio_sink",
    "composio_source",
    "connection_status",
    "initiate_connection",
    "is_transient_error",
    "list_connections",
    "load_trigger_cursor",
    "save_trigger_cursor",
    "wait_for_active",
]
