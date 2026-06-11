"""File handling helpers for the Composio connector.

The Composio SDK does the heavy lifting when file mode is enabled on the
client (see :class:`~aisquare_pipe_composio.client.ComposioClient`):

* tool **outputs** marked file-downloadable are downloaded under the
  client's download dir and replaced with local path strings in the result;
* tool **arguments** holding paths inside the client's upload dir are
  uploaded automatically.

This module finds those substituted download paths in a result, and
materialises envelope payloads as upload files.
"""

from __future__ import annotations

import mimetypes
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aisquare.pipe.core.envelope import DataEnvelope

from aisquare_pipe_composio.constants import DEFAULT_FILE_CONTENT_TYPE

_UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024


@dataclass
class DownloadedFile:
    """A file the SDK downloaded during tool execution."""

    field_path: str  # dot-path of the value inside the result data
    path: Path


def find_downloaded_files(data: Any, download_dir: Path | None) -> list[DownloadedFile]:
    """Walk a tool result and collect local files under ``download_dir``.

    After execution with file mode enabled, every file-downloadable leaf in
    the result is a string path inside the download dir — that containment
    is the marker (arbitrary strings elsewhere in the payload can't collide
    with a path under our managed directory unless the file really exists).
    """
    if download_dir is None:
        return []
    found: list[DownloadedFile] = []
    _walk_for_paths(data, str(download_dir), "", found)
    return found


def _walk_for_paths(
    value: Any, prefix: str, field_path: str, found: list[DownloadedFile]
) -> None:
    if isinstance(value, str):
        if value.startswith(prefix):
            path = Path(value)
            if path.is_file():
                found.append(DownloadedFile(field_path=field_path, path=path))
        return
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{field_path}.{key}" if field_path else str(key)
            _walk_for_paths(item, prefix, child, found)
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            child = f"{field_path}.{i}" if field_path else str(i)
            _walk_for_paths(item, prefix, child, found)


def guess_content_type(path: Path) -> str:
    """Best-effort MIME type for a downloaded file."""
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or DEFAULT_FILE_CONTENT_TYPE


def materialize_upload_file(envelope: DataEnvelope, upload_dir: Path) -> Path:
    """Write an envelope's binary payload into ``upload_dir`` and return the
    path. The caller is responsible for deleting the file after execution.

    The file lands inside the client's upload allowlist, which is the only
    location the SDK will auto-upload from.
    """
    filename = str(envelope.metadata.get("filename") or "upload.bin")
    # Strip any directory components from caller-supplied names.
    safe_name = Path(filename).name or "upload.bin"
    upload_dir.mkdir(parents=True, exist_ok=True)
    target = upload_dir / f"{uuid.uuid4().hex}-{safe_name}"

    if envelope.stream is not None:
        with target.open("wb") as fd:
            while True:
                chunk = envelope.stream.read(_UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                fd.write(chunk)
        return target

    data = envelope.data
    if isinstance(data, str):
        data = data.encode("utf-8")
    if not isinstance(data, bytes):
        raise ValueError(
            f"Cannot materialize envelope data of type {type(envelope.data).__name__} as a file"
        )
    target.write_bytes(data)
    return target
