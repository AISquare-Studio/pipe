"""Shared test helpers for creating mock Dropbox objects."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import dropbox.files


def make_file_metadata(
    name: str = "document.pdf",
    path: str = "/docs/document.pdf",
    size: int = 2048,
    file_id: str = "id:abc123",
    rev: str = "015f1234abcd",
) -> dropbox.files.FileMetadata:
    """Create a mock FileMetadata object."""
    meta = MagicMock(spec=dropbox.files.FileMetadata)
    meta.name = name
    meta.path_lower = path.lower()
    meta.size = size
    meta.id = file_id
    meta.rev = rev
    meta.server_modified = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    meta.client_modified = datetime(2025, 6, 15, 11, 0, 0, tzinfo=timezone.utc)
    meta.__class__ = dropbox.files.FileMetadata
    return meta


def make_folder_metadata(
    name: str = "docs",
    path: str = "/docs",
    folder_id: str = "id:folder1",
) -> dropbox.files.FolderMetadata:
    """Create a mock FolderMetadata object."""
    meta = MagicMock(spec=dropbox.files.FolderMetadata)
    meta.name = name
    meta.path_lower = path.lower()
    meta.id = folder_id
    meta.__class__ = dropbox.files.FolderMetadata
    return meta


def make_list_folder_result(entries, has_more=False, cursor="cursor-abc"):
    """Create a mock ListFolderResult."""
    result = MagicMock()
    result.entries = entries
    result.has_more = has_more
    result.cursor = cursor
    return result
