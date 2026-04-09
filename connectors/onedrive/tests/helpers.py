"""Shared test helpers for creating mock OneDrive/Graph API objects."""

from __future__ import annotations

from typing import Any


def make_file_item(
    name: str = "document.pdf",
    item_id: str = "item-abc123",
    size: int = 2048,
    mime_type: str = "application/pdf",
    path: str = "/drive/root:",
    web_url: str = "https://onedrive.live.com/...",
) -> dict[str, Any]:
    """Create a mock Graph API file item dict."""
    return {
        "id": item_id,
        "name": name,
        "size": size,
        "file": {"mimeType": mime_type},
        "parentReference": {"path": path},
        "lastModifiedDateTime": "2025-06-15T12:00:00Z",
        "createdDateTime": "2025-06-15T11:00:00Z",
        "webUrl": web_url,
    }


def make_folder_item(
    name: str = "Documents",
    item_id: str = "folder-abc123",
    path: str = "/drive/root:",
) -> dict[str, Any]:
    """Create a mock Graph API folder item dict."""
    return {
        "id": item_id,
        "name": name,
        "folder": {"childCount": 3},
        "parentReference": {"path": path},
        "lastModifiedDateTime": "2025-06-15T12:00:00Z",
        "webUrl": "https://onedrive.live.com/...",
    }


def make_graph_response(
    items: list[dict[str, Any]],
    next_link: str | None = None,
) -> dict[str, Any]:
    """Create a mock Graph API list response."""
    result: dict[str, Any] = {"value": items}
    if next_link:
        result["@odata.nextLink"] = next_link
    return result


def make_upload_response(
    name: str = "uploaded.txt",
    item_id: str = "item-new123",
    size: int = 100,
) -> dict[str, Any]:
    """Create a mock Graph API upload response."""
    return {
        "id": item_id,
        "name": name,
        "size": size,
        "lastModifiedDateTime": "2025-06-15T12:00:00Z",
        "webUrl": "https://onedrive.live.com/...",
    }
