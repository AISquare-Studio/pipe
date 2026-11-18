"""DatabricksFilesSource: the files under a volume or DBFS directory, as envelopes.

``config`` is the tenant's credential blob (:mod:`.credentials`). Pull params
pick the files, never the credential, the host or the warehouse:

* ``path`` (required): a directory (``/Volumes/<catalog>/<schema>/<volume>/...``
  or ``dbfs:/...``) or one file;
* ``recursive`` (default false): walk sub-directories too (at most
  ``MAX_DEPTH`` levels);
* ``modified_after`` (optional, RFC 3339): only files modified after it;
* ``max_files`` (default 100, at most 1000) and ``max_bytes`` per file
  (default 25 MiB, at most 100 MiB; a larger file is skipped and counted).

Each file becomes one envelope with its bytes and ``metadata`` ``filename``,
``path``, ``storage`` (volume | dbfs), ``size``, ``content_type`` and the
version markers ``modified_time``, ``etag`` (volumes) and ``sha256``. A
caller keeps them and re-reads the file when they change.

A param naming a credential key (``workspace_url``, ``token``, ...) is
refused. Skipped files are counted in one log line, with no paths.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from aisquare.pipe.core.connector import AuthType, SourceConnector
from aisquare.pipe.core.envelope import DataEnvelope, MetaField, PullParams

from aisquare_pipe_databricks.client import (
    DatabricksAPIError,
    DatabricksClient,
    FileTooLarge,
)
from aisquare_pipe_databricks.credentials import (
    SERVER_KEYS,
    DatabricksNotConfigured,
    load_credentials,
)
from aisquare_pipe_databricks.files import (
    BadPath,
    FilePath,
    file_envelope,
    list_entries,
    parse_path,
    read_file,
)
from aisquare_pipe_databricks.toolkit import DEFAULT_MAX_BYTES, MAX_FILE_BYTES

logger = logging.getLogger("aisquare.pipe.databricks")

DEFAULT_MAX_FILES = 100
MAX_FILES = 1000
LIST_PAGE_SIZE = 1000
MAX_DEPTH = 10
#: Directory pages followed at most per directory.
MAX_LIST_PAGES = 100
_PARAMS = frozenset({"path", "recursive", "modified_after", "max_files", "max_bytes"})


def _options(params: PullParams | dict[str, Any] | None) -> dict[str, Any]:
    if params is None:
        return {}
    raw = params.params if isinstance(params, PullParams) else params
    if not isinstance(raw, dict):
        raise TypeError("pull params must be a mapping")
    server = sorted(SERVER_KEYS.intersection(raw))
    if server:
        raise ValueError(
            f"pull param {server[0]!r} is server-controlled and cannot be set by a caller"
        )
    unknown = sorted(set(raw) - _PARAMS)
    if unknown:
        raise ValueError(f"unknown pull param {unknown[0]!r}")
    return raw


def _bounded(options: dict[str, Any], name: str, default: int, high: int) -> int:
    value = options.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= high:
        raise ValueError(f"{name} must be an integer from 1 to {high}")
    return value


def _timestamp(value: Any, what: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must be an RFC 3339 time")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(f"{what} must be an RFC 3339 time") from None
    if parsed.tzinfo is None:
        raise ValueError(f"{what} must carry a timezone (e.g. Z)")
    return parsed


def _after(modified: Any, since: datetime) -> bool:
    try:
        return _timestamp(modified, "modified_time") > since
    except ValueError:
        return True  # no usable time: keep it rather than lose a change


class DatabricksFilesSource(SourceConnector):
    """Pulls the files under a Unity Catalog volume or DBFS directory, with
    their version markers."""

    name = "databricks-files-source"
    version = "0.1.0"
    # Framework class attributes, declared plain on SourceConnector.
    output_types = ["*/*"]  # noqa: RUF012
    auth_type = AuthType.API_KEY
    description = (
        "Pulls the files under a Databricks Unity Catalog volume or DBFS directory, with "
        "their version markers (modified time, ETag, SHA-256)."
    )
    docs_url = "https://docs.databricks.com/api/workspace/files"

    CONFIG_SPEC: dict[str, MetaField] = {  # noqa: RUF012
        "workspace_url": MetaField(
            type=str,
            required=True,
            description="https://<workspace> (server-side only)",
        ),
        "auth": MetaField(
            type=str, required=False, default="pat", description="'pat' or 'oauth_m2m'"
        ),
        "token": MetaField(
            type=str, required=False, description="PAT (server-side only)"
        ),
        "client_id": MetaField(
            type=str,
            required=False,
            description="Service principal id (server-side only)",
        ),
        "client_secret": MetaField(
            type=str,
            required=False,
            description="Service principal secret (server-side only)",
        ),
        "warehouse_id": MetaField(
            type=str, required=False, description="SQL warehouse id (toolkit SQL only)"
        ),
    }

    metadata_spec = {  # noqa: RUF012
        "filename": MetaField(type=str, required=True, description="The file's name"),
        "path": MetaField(
            type=str, required=True, description="/Volumes/... or dbfs:/... path"
        ),
        "storage": MetaField(type=str, required=True, description="volume or dbfs"),
        "size": MetaField(type=int, required=True, description="Size in bytes"),
        "content_type": MetaField(type=str, required=False, description="Content type"),
        "modified_time": MetaField(
            type=str,
            required=False,
            description="RFC 3339 last-modified time: a version marker",
        ),
        "etag": MetaField(
            type=str, required=False, description="ETag (volumes): a version marker"
        ),
        "sha256": MetaField(
            type=str,
            required=False,
            description="SHA-256 of the bytes: a version marker",
        ),
    }

    def __init__(self, *, session: Any = None) -> None:
        self._session = session

    def validate_config(self, config: dict) -> bool:
        try:
            load_credentials(config)
        except DatabricksNotConfigured:
            return False
        return True

    def pull(
        self, config: dict, params: PullParams | None = None
    ) -> Iterator[DataEnvelope]:
        """Yield one envelope per file. Raises :class:`DatabricksNotConfigured`
        without a usable credential (before any request), ``ValueError`` for
        bad params, and :class:`DatabricksAPIError` when Databricks fails."""
        creds = load_credentials(config)
        options = _options(params)
        try:
            root = parse_path(options.get("path"), directory=True)
        except BadPath as e:
            raise ValueError(str(e)) from None
        recursive = options.get("recursive", False)
        if not isinstance(recursive, bool):
            raise ValueError("recursive must be true or false")  # noqa: TRY004 — every bad param is a ValueError
        after = options.get("modified_after")
        since = None if after is None else _timestamp(after, "modified_after")
        limit = _bounded(options, "max_files", DEFAULT_MAX_FILES, MAX_FILES)
        max_bytes = _bounded(options, "max_bytes", DEFAULT_MAX_BYTES, MAX_FILE_BYTES)

        with DatabricksClient(creds, session=self._session) as client:
            skipped = 0
            yielded = 0
            for entry in self._files(client, root, recursive=recursive, since=since):
                if yielded >= limit:
                    break
                size = entry.get("size")
                if isinstance(size, int) and size > max_bytes:
                    skipped += 1
                    continue
                where = FilePath(root.kind, str(entry["path"]))
                try:
                    content, record = read_file(client, where, max_bytes=max_bytes)
                except FileTooLarge:
                    skipped += 1
                    continue
                yielded += 1
                yield file_envelope(record, content, source_id=self.name)
            if skipped:
                logger.info(
                    "databricks-files-source: skipped %d file(s) larger than max_bytes",
                    skipped,
                )

    def _files(
        self,
        client: DatabricksClient,
        root: FilePath,
        *,
        recursive: bool,
        since: datetime | None,
    ) -> Iterator[dict[str, Any]]:
        stack: list[tuple[FilePath, int]] = [(root, 0)]
        while stack:
            where, depth = stack.pop(0)
            token: str | None = None
            for _ in range(MAX_LIST_PAGES):
                try:
                    entries, token = list_entries(
                        client, where, page_size=LIST_PAGE_SIZE, page_token=token
                    )
                except DatabricksAPIError as e:
                    # A path that is a file, not a directory: pull just it.
                    if depth == 0 and e.status in (400, 404):
                        yield {"path": where.path, "size": None, "modified_time": None}
                        return
                    raise
                for entry in entries:
                    if entry["is_directory"]:
                        if recursive and depth + 1 < MAX_DEPTH:
                            stack.append(
                                (
                                    FilePath(
                                        where.kind, str(entry["path"]).rstrip("/")
                                    ),
                                    depth + 1,
                                )
                            )
                        continue
                    if since is not None and not _after(
                        entry.get("modified_time"), since
                    ):
                        continue
                    yield entry
                if token is None:
                    break


__all__ = ["DatabricksAPIError", "DatabricksFilesSource", "DatabricksNotConfigured"]
