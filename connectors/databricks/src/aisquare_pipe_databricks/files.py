"""Files in Unity Catalog volumes and DBFS: paths, listings, reads, markers.

Two kinds of path, told apart by their form:

* ``/Volumes/<catalog>/<schema>/<volume>/...``: a Unity Catalog volume,
  through the Files API;
* ``dbfs:/...``: the workspace's DBFS, through the DBFS API, only when the
  tenant's credential says ``"dbfs": true`` (:class:`DbfsNotEnabled`
  otherwise: DBFS has no Unity Catalog grants, see :mod:`.credentials`).

Nothing else is accepted (no workspace files, no ``..`` or ``.`` segments, no
control characters, no empty segments).

Every file record carries its version markers: ``modified_time`` (RFC 3339
UTC), ``etag`` where the Files API sends one, and on a read ``sha256`` of the
bytes. A caller keeps them and re-reads the file when they change.
"""

from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from aisquare.pipe.core.envelope import DataEnvelope

from aisquare_pipe_databricks.client import DatabricksClient

VOLUME = "volume"
DBFS = "dbfs"
MAX_PATH_CHARS = 4096


class BadPath(ValueError):
    """A path that is not a volume or DBFS path this connector reads."""


class DbfsNotEnabled(BadPath):
    """A ``dbfs:/`` path, for a tenant whose credential doesn't enable DBFS."""


@dataclass(frozen=True)
class FilePath:
    kind: str  # VOLUME | DBFS
    path: str  # as the caller sees it: /Volumes/... or dbfs:/...

    @property
    def api_path(self) -> str:
        """The path the API takes: the volume path, or DBFS's ``/...``."""
        return self.path[len("dbfs:") :] if self.kind == DBFS else self.path


def parse_path(value: Any, *, directory: bool = False, dbfs: bool = False) -> FilePath:
    """``value`` as a :class:`FilePath`, or :class:`BadPath`; a ``dbfs:/``
    path only with ``dbfs`` (the credential's flag), else
    :class:`DbfsNotEnabled`."""
    if not isinstance(value, str) or not value.strip():
        raise BadPath("path is missing")
    path = value.strip()
    if len(path) > MAX_PATH_CHARS or not path.isprintable():
        raise BadPath("path is too long or holds control characters")
    if path.startswith("/Volumes/"):
        kind, rest = VOLUME, path[len("/Volumes/") :]
        if len([p for p in rest.split("/") if p]) < 3:
            raise BadPath("a volume path is /Volumes/<catalog>/<schema>/<volume>/...")
    elif path.startswith("dbfs:/"):
        if not dbfs:
            raise DbfsNotEnabled(
                "dbfs:/ paths are not enabled for this workspace (its Databricks "
                'credential has no "dbfs": true); read from a Unity Catalog volume, '
                "/Volumes/<catalog>/<schema>/<volume>/..."
            )
        kind, rest = DBFS, path[len("dbfs:/") :]
    else:
        raise BadPath(
            "path must be a volume path (/Volumes/...) or a DBFS path (dbfs:/...)"
        )
    segments = rest.rstrip("/").split("/") if rest.rstrip("/") else []
    if any(s in ("", ".", "..") for s in segments) or "\\" in rest:
        raise BadPath("path has an empty, '.' or '..' segment")
    if not directory and (not segments or path.endswith("/")):
        raise BadPath("path names a directory, not a file")
    return FilePath(kind, path.rstrip("/") if directory and len(path) > 6 else path)


def iso_from_millis(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    return (
        datetime.fromtimestamp(value / 1000, tz=UTC).isoformat().replace("+00:00", "Z")
    )


def iso_from_http_date(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def list_entries(
    client: DatabricksClient, where: FilePath, *, page_size: int, page_token: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    """One page of a directory: entries ``{path, name, is_directory, size,
    modified_time}`` and the next page token (DBFS lists in one page)."""
    if where.kind == VOLUME:
        body = client.list_directory(
            where.api_path, page_size=page_size, page_token=page_token
        )
        raw = body.get("contents")
        entries = [
            {
                "path": item.get("path"),
                "name": item.get("name"),
                "is_directory": bool(item.get("is_directory")),
                "size": item.get("file_size"),
                "modified_time": iso_from_millis(item.get("last_modified")),
            }
            for item in (raw if isinstance(raw, list) else [])
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        ]
        token = body.get("next_page_token")
        return entries, token if isinstance(token, str) and token else None
    body = client.dbfs_list(where.api_path)
    raw = body.get("files")
    entries = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            continue
        path = str(item["path"])
        entries.append(
            {
                "path": f"dbfs:{path}",
                "name": path.rstrip("/").rsplit("/", 1)[-1],
                "is_directory": bool(item.get("is_dir")),
                "size": item.get("file_size"),
                "modified_time": iso_from_millis(item.get("modification_time")),
            }
        )
    return entries, None


def file_metadata(client: DatabricksClient, where: FilePath) -> dict[str, Any]:
    """A file's record without its bytes: ``path, name, size, content_type,
    modified_time, etag``."""
    if where.kind == VOLUME:
        headers = client.file_metadata(where.api_path)
        return _record(
            where,
            headers.get("size"),
            headers.get("content_type"),
            iso_from_http_date(headers.get("last_modified")),
            headers.get("etag"),
        )
    status = client.dbfs_status(where.api_path)
    if status.get("is_dir"):
        raise BadPath("path names a directory, not a file")
    size = status.get("file_size")
    return _record(
        where,
        size if isinstance(size, int) else None,
        None,
        iso_from_millis(status.get("modification_time")),
        None,
    )


def read_file(
    client: DatabricksClient, where: FilePath, *, max_bytes: int
) -> tuple[bytes, dict[str, Any]]:
    """A file's bytes and its record (``sha256`` of the bytes added)."""
    if where.kind == VOLUME:
        content, headers = client.download_file(where.api_path, max_bytes=max_bytes)
        record = _record(
            where,
            len(content),
            headers.get("content_type"),
            iso_from_http_date(headers.get("last_modified")),
            headers.get("etag"),
        )
    else:
        record = file_metadata(client, where)
        size = record.get("size") if isinstance(record.get("size"), int) else 0
        content = client.dbfs_read(
            where.api_path, size=int(size or 0), max_bytes=max_bytes
        )
        record["size"] = len(content)
    record["sha256"] = hashlib.sha256(content).hexdigest()
    return content, record


def _record(
    where: FilePath, size: Any, content_type: Any, modified: str | None, etag: Any
) -> dict[str, Any]:
    name = where.path.rstrip("/").rsplit("/", 1)[-1]
    if (
        not isinstance(content_type, str)
        or not content_type
        or content_type == ("application/octet-stream")
    ):
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return {
        "path": where.path,
        "name": name,
        "storage": where.kind,
        "size": size if isinstance(size, int) else None,
        "content_type": content_type,
        "modified_time": modified,
        "etag": etag if isinstance(etag, str) and etag else None,
    }


def file_envelope(
    record: dict[str, Any], content: bytes, *, source_id: str
) -> DataEnvelope:
    """A file's bytes as an envelope whose metadata carries its version markers."""
    metadata = {
        "filename": record.get("name"),
        "path": record.get("path"),
        "storage": record.get("storage"),
        "size": len(content),
        "content_type": record.get("content_type"),
        **{
            key: record[key]
            for key in ("modified_time", "etag", "sha256")
            if record.get(key) is not None
        },
    }
    return DataEnvelope(
        content_type=str(record.get("content_type") or "application/octet-stream"),
        data=content,
        source_id=source_id,
        metadata=metadata,
    )


__all__ = [
    "DBFS",
    "VOLUME",
    "BadPath",
    "DbfsNotEnabled",
    "FilePath",
    "file_envelope",
    "file_metadata",
    "list_entries",
    "parse_path",
    "read_file",
]
