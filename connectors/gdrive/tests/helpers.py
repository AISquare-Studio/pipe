"""Shared test helpers for building mock Drive API payloads."""

from __future__ import annotations

from typing import Any


def make_file(
    name: str = "document.pdf",
    file_id: str = "file-abc123",
    mime_type: str = "application/pdf",
    size: int = 2048,
    parents: list[str] | None = None,
) -> dict[str, Any]:
    """Create a mock Drive API file resource."""
    return {
        "id": file_id,
        "name": name,
        "mimeType": mime_type,
        "size": str(size),
        "modifiedTime": "2026-06-15T12:00:00.000Z",
        "md5Checksum": "d41d8cd98f00b204e9800998ecf8427e",
        "parents": parents or ["root"],
        "webViewLink": f"https://drive.google.com/file/d/{file_id}/view",
    }


def make_folder(
    name: str = "Documents",
    file_id: str = "folder-abc123",
) -> dict[str, Any]:
    """Create a mock Drive API folder resource. Folders carry no size."""
    return {
        "id": file_id,
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "modifiedTime": "2026-06-15T12:00:00.000Z",
        "parents": ["root"],
        "webViewLink": f"https://drive.google.com/drive/folders/{file_id}",
    }


def make_native(
    name: str = "Report",
    file_id: str = "doc-abc123",
    mime_type: str = "application/vnd.google-apps.document",
) -> dict[str, Any]:
    """Create a mock Google-native file. Native files report no size."""
    return {
        "id": file_id,
        "name": name,
        "mimeType": mime_type,
        "modifiedTime": "2026-06-15T12:00:00.000Z",
        "parents": ["root"],
        "webViewLink": f"https://docs.google.com/document/d/{file_id}/edit",
    }


def make_list_response(
    files: list[dict[str, Any]],
    next_page_token: str | None = None,
) -> dict[str, Any]:
    """Create a mock files.list response."""
    result: dict[str, Any] = {"files": files}
    if next_page_token:
        result["nextPageToken"] = next_page_token
    return result
