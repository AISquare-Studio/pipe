"""Composio meta-connector for aisquare.pipe — 500+ SaaS toolkits through one connector."""

from aisquare_pipe_composio.connections import (
    ConnectionRequest,
    connection_status,
    initiate_connection,
    list_connections,
    wait_for_active,
)
from aisquare_pipe_composio.connector import ComposioSink, ComposioSource
from aisquare_pipe_composio.factory import composio_sink, composio_source
from aisquare_pipe_composio.triggers import ComposioTriggersSource

__all__ = [
    "ComposioSink",
    "ComposioSource",
    "ComposioTriggersSource",
    "ConnectionRequest",
    "composio_sink",
    "composio_source",
    "connection_status",
    "initiate_connection",
    "list_connections",
    "wait_for_active",
]
