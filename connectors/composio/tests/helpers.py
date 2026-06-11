"""Test data builders for the Composio connector tests."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any


def make_toolkit(slug: str = "gmail", name: str | None = None, **meta: Any) -> dict:
    return {
        "slug": slug,
        "name": name or slug.capitalize(),
        "no_auth": False,
        "meta": {"description": f"{slug} toolkit", "tools_count": 10, **meta},
    }


def make_tool(slug: str, toolkit: str = "gmail", description: str = "") -> dict:
    return {"slug": slug, "name": slug, "description": description, "toolkit": {"slug": toolkit}}


def make_account(
    toolkit: str = "gmail",
    status: str = "ACTIVE",
    account_id: str = "ca_123",
) -> dict:
    return {"id": account_id, "status": status, "toolkit": {"slug": toolkit}}


def make_event(
    event_id: str = "evt_1",
    slug: str = "GMAIL_NEW_GMAIL_MESSAGE",
    toolkit: str = "gmail",
    ts_ms: int = 1_700_000_000_000,
    payload: Any = None,
    **overrides: Any,
) -> dict:
    """A normalized trigger event, as ComposioClient.list_trigger_events
    returns them."""
    event = {
        "id": event_id,
        "trigger_slug": slug,
        "trigger_id": f"ti_{event_id}",
        "toolkit": toolkit,
        "connected_account_id": "ca_123",
        "user_id": "test-user",
        "timestamp": "2023-11-14T22:13:20Z",
        "timestamp_ms": ts_ms,
        "status": "success",
        "payload": payload if payload is not None else {"subject": "hi"},
    }
    event.update(overrides)
    return event


def make_raw_log_item(
    event_id: str = "evt_1",
    trigger_name: str = "GMAIL_NEW_GMAIL_MESSAGE",
    app_name: str = "gmail",
    created_at: str = "2023-11-14T22:13:20Z",
    payload: Any = None,
) -> dict:
    """A raw trigger-log item, as the Composio logs API returns them."""
    return {
        "id": event_id,
        "app_name": app_name,
        "client_id": "cl_1",
        "connection_id": "ca_123",
        "created_at": created_at,
        "entity_id": "test-user",
        "status": "success",
        "type": "trigger",
        "meta": {
            "trigger_name": trigger_name,
            "trigger_nano_id": f"ti_{event_id}",
            "trigger_provider_payload": json.dumps(
                payload if payload is not None else {"subject": "hi"}
            ),
        },
    }


def make_list_response(items: list, next_cursor: str | None = None) -> SimpleNamespace:
    """Shape of a Stainless list response (items + next_cursor)."""
    return SimpleNamespace(items=items, next_cursor=next_cursor)


def make_log_response(data: list, next_cursor: str | None = None) -> SimpleNamespace:
    """Shape of the trigger-logs list response (data + next_cursor)."""
    return SimpleNamespace(data=data, next_cursor=next_cursor)
