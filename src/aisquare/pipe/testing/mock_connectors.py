"""Mock connectors (and a mock toolkit) for testing the aisquare.pipe framework."""

from __future__ import annotations

import copy
from collections.abc import Iterator
from typing import Any

from aisquare.pipe.core.connector import AuthType, SinkConnector, SourceConnector
from aisquare.pipe.core.envelope import (
    DataEnvelope,
    MetaField,
    PullParams,
    PushParams,
    PushResult,
)
from aisquare.pipe.core.oauth import OAuthSpec
from aisquare.pipe.core.tools import Toolkit, ToolResult, ToolSpec
from aisquare.pipe.core.types import TypeConverter
from aisquare.pipe.errors import ToolError


class MockSource(SourceConnector):
    """Generates fake envelopes for testing."""

    name = "mock-source"
    version = "0.1.0"
    output_types = ["text/plain", "application/json", "image/png"]
    auth_type = AuthType.NONE
    description = "Mock source connector for testing"
    metadata_spec = {
        "filename": MetaField(type=str, required=False, description="Filename"),
        "index": MetaField(type=int, required=False, description="Item index"),
    }

    def __init__(
        self,
        envelopes: list[DataEnvelope] | None = None,
        count: int = 3,
    ) -> None:
        self._envelopes = envelopes
        self._count = count

    def pull(
        self, config: dict, params: PullParams | None = None
    ) -> Iterator[DataEnvelope]:
        if self._envelopes is not None:
            yield from self._envelopes
        else:
            for i in range(self._count):
                yield DataEnvelope(
                    content_type="text/plain",
                    data=f"Mock data item {i}",
                    source_id="mock-source",
                    metadata={"filename": f"item_{i}.txt", "index": i},
                )

    def validate_config(self, config: dict) -> bool:
        return True


class MockSink(SinkConnector):
    """Collects envelopes in memory for inspection."""

    name = "mock-sink"
    version = "0.1.0"
    input_types = ["text/plain", "application/json"]
    auth_type = AuthType.NONE
    description = "Mock sink connector for testing"

    def __init__(self) -> None:
        self.received: list[DataEnvelope] = []

    def push(
        self,
        envelope: DataEnvelope,
        config: dict,
        params: PushParams | None = None,
    ) -> PushResult:
        self.received.append(envelope)
        return PushResult(success=True, ref=f"mock-{len(self.received)}")

    def validate_config(self, config: dict) -> bool:
        return True


class MockConverter(TypeConverter):
    """Converts text/plain to application/json by wrapping text in {"text": ...}."""

    from_type = "text/plain"
    to_type = "application/json"

    def convert(self, envelope: DataEnvelope) -> DataEnvelope:
        text = (
            envelope.data
            if isinstance(envelope.data, str)
            else envelope.data.decode() if isinstance(envelope.data, bytes) else str(envelope.data)
        )
        return DataEnvelope(
            content_type="application/json",
            data={"text": text},
            metadata=envelope.metadata.copy(),
            source_id=envelope.source_id,
        )


# --------------------------------------------------------------------------
# Mock toolkit
# --------------------------------------------------------------------------

#: OAuth spec of the mock toolkit. The endpoints are never contacted by the
#: toolkit itself; tests mock urllib when they drive the OAuth flow.
MOCK_OAUTH = OAuthSpec(
    provider="mock",
    authorize_url="https://auth.mock.example/oauth/authorize",
    token_url="https://auth.mock.example/oauth/token",
    revoke_url="https://auth.mock.example/oauth/revoke",
    default_scopes=("items:read", "items:write"),
    pkce=True,
    token_expires=True,
    rotates_refresh_token=True,
)

_ITEM_ID = {"type": "string", "description": "Id of the item."}

MOCK_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        slug="MOCK_LIST_ITEMS",
        description="List items in the mock store, optionally filtered by a title substring.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Case-insensitive title substring."},
                "status": {"type": "string", "enum": ["open", "closed"]},
                "limit": {"type": "integer", "default": 10, "description": "Maximum items."},
            },
        },
        read_only=True,
        tags=("items",),
    ),
    ToolSpec(
        slug="MOCK_GET_ITEM",
        description="Get one item by id.",
        input_schema={
            "type": "object",
            "properties": {"item_id": _ITEM_ID},
            "required": ["item_id"],
        },
        read_only=True,
        tags=("items",),
    ),
    ToolSpec(
        slug="MOCK_CREATE_ITEM",
        description="Create an item with a title and an optional status.",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "status": {"type": "string", "enum": ["open", "closed"], "default": "open"},
            },
            "required": ["title"],
        },
        read_only=False,
        tags=("items",),
    ),
    ToolSpec(
        slug="MOCK_DELETE_ITEM",
        description="Permanently delete an item by id.",
        input_schema={
            "type": "object",
            "properties": {"item_id": _ITEM_ID},
            "required": ["item_id"],
        },
        read_only=False,
        destructive=True,
        tags=("items",),
    ),
    ToolSpec(
        slug="MOCK_DOWNLOAD_FILE",
        description="Download the file attached to an item; the bytes come back in ToolResult.files.",
        input_schema={
            "type": "object",
            "properties": {"item_id": _ITEM_ID},
            "required": ["item_id"],
        },
        read_only=True,
        tags=("files",),
    ),
    ToolSpec(
        slug="MOCK_GET_AUTHENTICATED_USER",
        description="Get the profile of the authenticated user (the owner of the access token).",
        input_schema={"type": "object", "properties": {}},
        read_only=True,
        tags=("users",),
    ),
)

_SEED_ITEMS: dict[str, dict[str, Any]] = {
    "1": {"id": "1", "title": "First item", "status": "open"},
    "2": {"id": "2", "title": "Second item", "status": "closed"},
}


class MockToolkit(Toolkit):
    """In-memory toolkit for hermetic tests: items CRUD, a file download and
    a ``whoami``. Any non-empty ``access_token`` authenticates."""

    name = "mock"
    version = "0.1.0"
    display_name = "Mock"
    description = "In-memory mock toolkit for testing"
    oauth = MOCK_OAUTH

    def __init__(self, items: dict[str, dict[str, Any]] | None = None) -> None:
        self.items = copy.deepcopy(_SEED_ITEMS if items is None else items)

    def tools(self) -> list[ToolSpec]:
        return list(MOCK_TOOLS)

    def run_tool(
        self, tool: ToolSpec, arguments: dict[str, Any], config: dict[str, Any]
    ) -> ToolResult:
        if tool.slug == "MOCK_LIST_ITEMS":
            query = str(arguments.get("query", "")).lower()
            found = [
                item
                for item in self.items.values()
                if query in item["title"].lower()
                and arguments.get("status") in (None, item["status"])
            ]
            return ToolResult.ok({"items": found[: arguments["limit"]]})
        if tool.slug == "MOCK_CREATE_ITEM":
            item_id = str(max((int(k) for k in self.items), default=0) + 1)
            created = {"id": item_id, "title": arguments["title"], "status": arguments["status"]}
            self.items[item_id] = created
            return ToolResult.ok(created)
        if tool.slug == "MOCK_GET_AUTHENTICATED_USER":
            return ToolResult.ok({"login": "mock-user", "id": 1})

        item = self.items.get(str(arguments["item_id"]))
        if item is None:
            raise ToolError("item not found")
        if tool.slug == "MOCK_GET_ITEM":
            return ToolResult.ok(item)
        if tool.slug == "MOCK_DELETE_ITEM":
            del self.items[item["id"]]
            return ToolResult.ok({"deleted": True, "id": item["id"]})
        if tool.slug == "MOCK_DOWNLOAD_FILE":
            filename = f"item-{item['id']}.txt"
            content = f"{item['title']}\n".encode()
            envelope = DataEnvelope(
                content_type="text/plain",
                data=content,
                source_id=self.name,
                metadata={"filename": filename, "item_id": item["id"]},
            )
            return ToolResult.ok(
                {"filename": filename, "mime_type": "text/plain", "size": len(content)},
                files=[envelope],
            )
        raise ToolError(f"{tool.slug} is not implemented")
