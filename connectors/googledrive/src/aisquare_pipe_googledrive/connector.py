"""Google Drive source and sink connectors for aisquare.pipe.

Both talk to Drive v3 through :class:`~aisquare_pipe_googledrive.rest.GoogleDriveRestClient`
(the package's only HTTP boundary) with the ``access_token`` from ``config``.
"""

from __future__ import annotations

import json
import logging
import os
import posixpath
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from typing import IO, Any, cast

import requests
from aisquare.pipe.core.connector import AuthType, SinkConnector, SourceConnector
from aisquare.pipe.core.envelope import (
    DataEnvelope,
    MetaField,
    PullParams,
    PushParams,
    PushResult,
    RateLimit,
    Resource,
)
from aisquare.pipe.errors import ConfigValidationError, PipeError, PipelineError

from aisquare_pipe_googledrive.formats import (
    SOURCE_EXPORTS,
    exported_name,
    guess_mimetype,
    is_native,
)
from aisquare_pipe_googledrive.rest import (
    FOLDER_MIME,
    GoogleDriveAPIError,
    GoogleDriveRestClient,
    escape_query,
    group_query,
    json_body,
)
from aisquare_pipe_googledrive.toolkit import describe_error, error_code

logger = logging.getLogger("aisquare.pipe.googledrive")

DOCS_URL = "https://developers.google.com/drive/api/reference/rest/v3"
#: Files larger than this are handed on as a stream instead of buffered bytes.
STREAM_THRESHOLD = 50 * 1024 * 1024
_PAGE_SIZE = 1000
_ITEM_FIELDS = "id,name,mimeType,size,modifiedTime,webViewLink,md5Checksum,driveId"
_CONFLICTS = ("rename", "replace", "fail", "keep_both")
_NOT_CONNECTED = "Google Drive is not connected: config needs an 'access_token'"


def _token(config: Mapping[str, Any] | None) -> str | None:
    """The config's usable access token, or None (offline shape check)."""
    token = config.get("access_token") if isinstance(config, Mapping) else None
    if not isinstance(token, str):
        return None
    token = token.strip()
    return token if token and not any(ch.isspace() for ch in token) else None


def _as_error(e: GoogleDriveAPIError) -> PipeError:
    """A Drive failure as a pipeline error: a rejected token is a config
    problem (reconnect), everything else a pipeline failure."""
    if e.status == 401:
        return ConfigValidationError(describe_error(e))
    return PipelineError(describe_error(e))


def _all_drives() -> dict[str, str]:
    return {"supportsAllDrives": "true", "includeItemsFromAllDrives": "true", "corpora": "allDrives"}


def _segments(path: str | None) -> list[str]:
    parts = [p for p in (path or "").replace("\\", "/").split("/") if p]
    if any(p in (".", "..") for p in parts):
        raise ValueError("path must not contain '.' or '..' segments")
    return parts


def _list_all(client: GoogleDriveRestClient, q: str, fields: str, order_by: str | None = None) -> Iterator[dict[str, Any]]:
    """Every file matching ``q``, following ``nextPageToken``."""
    params: dict[str, Any] = {
        "q": q,
        "fields": f"nextPageToken,files({fields})",
        "pageSize": _PAGE_SIZE,
        **_all_drives(),
    }
    if order_by:
        params["orderBy"] = order_by
    while True:
        data = json_body(client.get("/files", params)) or {}
        yield from data.get("files") or []
        token = data.get("nextPageToken")
        if not token:
            return
        params["pageToken"] = token


def _child_folder(client: GoogleDriveRestClient, parent: str, name: str) -> str | None:
    q = (
        f"'{escape_query(parent)}' in parents and name = '{escape_query(name)}' "
        f"and mimeType = '{FOLDER_MIME}' and trashed = false"
    )
    found = list(_list_all(client, q, "id,name", order_by="createdTime"))
    if len(found) > 1:
        logger.warning("googledrive: %d folders share one name; using the oldest", len(found))
    return str(found[0]["id"]) if found else None


def resolve_folder(
    client: GoogleDriveRestClient, root: str, path: str | None, *, create: bool = False
) -> str:
    """The id of folder ``path`` (``'Reports/2026'``) under folder ``root``,
    creating missing folders when ``create``."""
    folder = root
    for name in _segments(path):
        child = _child_folder(client, folder, name)
        if child is None:
            if not create:
                raise PipelineError(f"Google Drive folder not found: {name!r} (in path {path!r})")
            created = client.request(
                "POST",
                "/files",
                params={"supportsAllDrives": "true", "fields": "id"},
                json={"name": name, "mimeType": FOLDER_MIME, "parents": [folder]},
            )
            child = str((json_body(created) or {}).get("id"))
        folder = child
    return folder


def _matches_mime(patterns: list[str] | None, *types: str) -> bool:
    if not patterns:
        return True
    for pattern in patterns:
        pattern = pattern.lower()
        for mime in types:
            mime = mime.lower()
            if pattern == mime or (pattern.endswith("/*") and mime.startswith(pattern[:-1])):
                return True
    return False


class _Connector:
    """Shared construction: tests inject the HTTP session and sleep."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._session = session
        self._sleep = sleep

    def _client(self, token: str) -> GoogleDriveRestClient:
        return GoogleDriveRestClient(token, session=self._session, sleep=self._sleep)


class GoogleDriveSource(_Connector, SourceConnector):
    """Pull files from a Google Drive folder (My Drive or a shared drive).

    Google Docs, Sheets, Slides, Drawings and Apps Script files are exported
    (defaults: Docs -> DOCX, Sheets -> CSV of the first tab, Slides -> PDF,
    Drawings -> PNG; override per type with ``export_formats``). Folders,
    shortcuts and types Drive cannot export (Forms, Sites, ...) are skipped.
    """

    name = "googledrive-source"
    version = "0.3.0"
    output_types = ["*/*"]  # noqa: RUF012 — framework contract: class-level spec, never mutated
    auth_type = AuthType.OAUTH2
    description = "Pull files from Google Drive (Google Docs, Sheets and Slides exported)"
    docs_url = DOCS_URL
    metadata_spec = {  # noqa: RUF012 — framework contract: class-level spec, never mutated
        "filename": MetaField(type=str, required=True, description="File name as delivered (export extension added)"),
        "file_id": MetaField(type=str, required=True, description="Google Drive file id"),
        "mime_type": MetaField(type=str, required=True, description="MIME type of the delivered bytes"),
        "path": MetaField(type=str, required=True, description="Path below the pulled folder, e.g. 'Reports/q1.pdf'"),
        "name": MetaField(type=str, required=False, description="The file's name in Drive"),
        "source_mime_type": MetaField(type=str, required=False, description="MIME type in Drive (differs when exported)"),
        "exported": MetaField(type=bool, required=False, description="True for an exported Google Docs/Sheets/Slides file"),
        "size": MetaField(type=int, required=False, description="Size in bytes, when known"),
        "modified_time": MetaField(type=str, required=False, description="RFC 3339 last-modified time"),
        "web_view_link": MetaField(type=str, required=False, description="Link to the file in Drive"),
        "drive_id": MetaField(type=str, required=False, description="Shared drive id, when the file is in one"),
        "md5_checksum": MetaField(type=str, required=False, description="Drive's MD5 of a stored file"),
    }

    def pull(self, config: dict, params: PullParams | None = None) -> Iterator[DataEnvelope]:
        """Yield one envelope per file in the folder.

        Supported PullParams keys:
            folder_id (str): Folder to pull (default ``root``; a shared drive's
                id is its root folder). ``drive_id`` is the same for shared drives.
            path (str): Folder path below ``folder_id``, e.g. ``Reports/2026``.
            recursive (bool): Descend into subfolders (default False).
            mime_types (list[str]): Only these types (Drive's or the exported
                one; ``image/*`` wildcards allowed).
            extensions (list[str]): Only these file extensions, e.g. ``[".pdf"]``.
            modified_after (str): Only files modified after this RFC 3339 time.
            query (str): Extra Drive query clause ANDed onto each listing.
            export_formats (dict[str, str]): Google type -> export MIME type.
            limit (int): Stop after this many files.
            stream_threshold (int): Byte size above which a file is streamed.
        """
        options: Mapping[str, Any] = params.params if isinstance(params, PullParams) else (params or {})
        return self._pull(config, options)

    def _pull(self, config: dict, options: Mapping[str, Any]) -> Iterator[DataEnvelope]:
        token = _token(config)
        if token is None:
            raise ConfigValidationError(_NOT_CONNECTED)
        root = str(options.get("folder_id") or options.get("drive_id") or "root")
        recursive = bool(options.get("recursive", False))
        limit = options.get("limit")
        exports = {**SOURCE_EXPORTS, **(options.get("export_formats") or {})}
        mime_types = options.get("mime_types")
        extensions = [e.lower() for e in options.get("extensions") or []]
        threshold = int(options.get("stream_threshold") or STREAM_THRESHOLD)
        filters = []
        if options.get("modified_after"):
            filters.append(f"modifiedTime > '{escape_query(str(options['modified_after']))}'")
        if options.get("query"):
            filters.append(group_query(str(options["query"])))

        count = 0
        with self._client(token) as client:
            try:
                start = resolve_folder(client, root, options.get("path"))
                for item, prefix in _walk(client, start, recursive, filters):
                    if limit is not None and count >= int(limit):
                        return
                    envelope = self._envelope(client, item, prefix, exports, mime_types, extensions, threshold)
                    if envelope is None:
                        continue
                    count += 1
                    yield envelope
            except GoogleDriveAPIError as e:
                raise _as_error(e) from e

    def _envelope(
        self,
        client: GoogleDriveRestClient,
        item: dict[str, Any],
        prefix: str,
        exports: Mapping[str, str],
        mime_types: list[str] | None,
        extensions: list[str],
        threshold: int,
    ) -> DataEnvelope | None:
        source_mime = str(item.get("mimeType") or "")
        name = str(item.get("name") or item.get("id"))
        export_mime: str | None = None
        if is_native(source_mime):
            export_mime = exports.get(source_mime)
            if not export_mime:
                logger.debug("googledrive: skipping a %s (not exportable)", source_mime)
                return None
            filename = exported_name(name, export_mime)
            content_type = export_mime
        else:
            filename = name
            content_type = source_mime or guess_mimetype(name)
        if not _matches_mime(mime_types, source_mime, content_type):
            return None
        if extensions and posixpath.splitext(filename)[1].lower() not in extensions:
            return None

        size = int(item["size"]) if str(item.get("size") or "").isdigit() else None
        metadata: dict[str, Any] = {
            "filename": filename,
            "file_id": item.get("id"),
            "mime_type": content_type,
            "path": f"{prefix}/{filename}" if prefix else filename,
            "name": name,
            "source_mime_type": source_mime,
            "exported": export_mime is not None,
            "modified_time": item.get("modifiedTime", ""),
            "web_view_link": item.get("webViewLink", ""),
        }
        for key, field in (("drive_id", "driveId"), ("md5_checksum", "md5Checksum")):
            if item.get(field):
                metadata[key] = item[field]

        file_id = str(item.get("id"))
        if export_mime is None and size is not None and size > threshold:
            response = client.download(file_id, stream=True)
            raw = response.raw
            raw.decode_content = True  # hand on the file's bytes, not a gzip transfer encoding
            metadata["size"] = size
            return DataEnvelope(
                content_type=content_type,
                data=b"",
                source_id=self.name,
                stream=cast(IO[bytes], raw),
                metadata=metadata,
            )
        content = client.download(file_id, export_mime=export_mime).content
        metadata["size"] = len(content)
        return DataEnvelope(content_type=content_type, data=content, source_id=self.name, metadata=metadata)

    def validate_config(self, config: dict) -> bool:
        """Offline shape check: a non-empty ``access_token`` (no network)."""
        return _token(config) is not None

    def list_resources(self, config: dict) -> list[Resource]:
        """The top of My Drive (folders and files) plus the user's shared drives."""
        token = _token(config)
        if token is None:
            raise ConfigValidationError(_NOT_CONNECTED)
        resources: list[Resource] = []
        with self._client(token) as client:
            try:
                q = "'root' in parents and trashed = false"
                for item in _list_all(client, q, _ITEM_FIELDS, order_by="folder,name"):
                    folder = item.get("mimeType") == FOLDER_MIME
                    meta = {
                        "mime_type": item.get("mimeType", ""),
                        "modified_time": item.get("modifiedTime", ""),
                        "web_view_link": item.get("webViewLink", ""),
                    }
                    if not folder and str(item.get("size") or "").isdigit():
                        meta["size"] = int(item["size"])
                    resources.append(
                        Resource(
                            id=str(item.get("id")),
                            name=str(item.get("name", "")),
                            resource_type="folder" if folder else "file",
                            metadata=meta,
                        )
                    )
                drives = json_body(client.get("/drives", {"pageSize": 100, "fields": "drives(id,name)"})) or {}
                for drive in drives.get("drives") or []:
                    resources.append(
                        Resource(id=str(drive.get("id")), name=str(drive.get("name", "")), resource_type="shared_drive")
                    )
            except GoogleDriveAPIError as e:
                raise _as_error(e) from e
        return resources

    def supports_streaming(self) -> bool:
        return True

    def rate_limit(self) -> RateLimit | None:
        return RateLimit(requests_per_second=10, concurrent=4)


def _walk(
    client: GoogleDriveRestClient, root: str, recursive: bool, filters: list[str]
) -> Iterator[tuple[dict[str, Any], str]]:
    """``(file, folder path)`` for each file under ``root``, breadth-first;
    each folder is visited once (Drive folders can be reached twice)."""
    queue: deque[tuple[str, str]] = deque([(root, "")])
    seen = {root}
    while queue:
        folder, prefix = queue.popleft()
        parent = f"'{escape_query(folder)}' in parents and trashed = false"
        files_q = " and ".join([parent, f"mimeType != '{FOLDER_MIME}'", *filters])
        for item in _list_all(client, files_q, _ITEM_FIELDS, order_by="name"):
            yield item, prefix
        if not recursive:
            continue
        folders_q = f"{parent} and mimeType = '{FOLDER_MIME}'"
        for sub in _list_all(client, folders_q, "id,name", order_by="name"):
            sub_id = str(sub.get("id"))
            if sub_id in seen:
                continue
            seen.add(sub_id)
            name = str(sub.get("name", sub_id))
            queue.append((sub_id, f"{prefix}/{name}" if prefix else name))


class GoogleDriveSink(_Connector, SinkConnector):
    """Upload envelopes to a Google Drive folder (My Drive or a shared drive).

    Up to 5 MB goes as one multipart upload, anything larger (or a stream)
    through a resumable session.
    """

    name = "googledrive-sink"
    version = "0.3.0"
    input_types = ["*/*"]  # noqa: RUF012 — framework contract: class-level spec, never mutated
    auth_type = AuthType.OAUTH2
    description = "Push files to Google Drive"
    docs_url = DOCS_URL
    metadata_spec = {  # noqa: RUF012 — framework contract: class-level spec, never mutated
        "filename": MetaField(type=str, required=True, description="Name for the file in Drive"),
        "size": MetaField(type=int, required=False, description="Byte size of a streamed envelope"),
    }

    def push(self, envelope: DataEnvelope, config: dict, params: PushParams | None = None) -> PushResult:
        """Upload ``envelope`` as one file.

        Supported PushParams keys:
            folder_id (str): Destination folder (default ``root``).
            path (str): Folder path below ``folder_id``; missing folders are
                created unless ``create_folders`` is False.
            conflict (str): When a file of that name exists: ``rename``
                (default; uploads as ``name (1).ext``), ``replace`` (new
                revision of the existing file), ``fail``, or ``keep_both``.
            convert_to (str): Google type to convert into on upload, e.g.
                ``application/vnd.google-apps.document`` for a DOCX.
            description (str): The new file's description.
        """
        options: Mapping[str, Any] = params.params if isinstance(params, PushParams) else (params or {})
        token = _token(config)
        if token is None:
            return PushResult(success=False, error=_NOT_CONNECTED)
        conflict = str(options.get("conflict") or "rename")
        if conflict not in _CONFLICTS:
            return PushResult(success=False, error=f"conflict must be one of {', '.join(_CONFLICTS)}")
        metadata = envelope.metadata if isinstance(envelope.metadata, Mapping) else {}
        filename = str(metadata.get("filename") or metadata.get("name") or "unnamed_file")
        try:
            content, size, content_type = _payload(envelope, filename)
            with self._client(token) as client:
                folder = resolve_folder(
                    client,
                    str(options.get("folder_id") or "root"),
                    options.get("path"),
                    create=options.get("create_folders", True) is not False,
                )
                return self._upload(client, folder, filename, content, size, content_type, conflict, options)
        except GoogleDriveAPIError as e:
            logger.warning("googledrive: push failed (HTTP %s)", e.status)
            result_meta: dict[str, Any] = {"status": e.status, "error_code": error_code(e)}
            if e.retry_after is not None:
                result_meta["retry_after"] = e.retry_after
            return PushResult(success=False, error=describe_error(e), metadata=result_meta)
        except (PipelineError, ValueError) as e:
            return PushResult(success=False, error=str(e))
        except Exception as e:  # noqa: BLE001 — push reports, never raises
            logger.warning("googledrive: push failed (%s)", type(e).__name__)
            return PushResult(success=False, error=f"Google Drive push failed ({type(e).__name__})")

    def _upload(
        self,
        client: GoogleDriveRestClient,
        folder: str,
        filename: str,
        content: bytes | IO[bytes],
        size: int | None,
        content_type: str,
        conflict: str,
        options: Mapping[str, Any],
    ) -> PushResult:
        existing: list[dict[str, Any]] = []
        if conflict != "keep_both":
            q = (
                f"'{escape_query(folder)}' in parents and name = '{escape_query(filename)}' "
                f"and mimeType != '{FOLDER_MIME}' and trashed = false"
            )
            existing = list(_list_all(client, q, "id,name"))
        action = "created"
        file_id: str | None = None
        name = filename
        if existing and conflict == "fail":
            return PushResult(success=False, error=f"a file named {filename!r} already exists in the folder")
        if existing and conflict == "replace":
            file_id, action = str(existing[0]["id"]), "replaced"
        elif existing and conflict == "rename":
            name, action = _free_name(client, folder, filename), "renamed"

        meta: dict[str, Any] = {}
        if file_id is None:
            meta = {"name": name, "parents": [folder]}
            if options.get("description"):
                meta["description"] = str(options["description"])
        if options.get("convert_to"):
            meta["mimeType"] = str(options["convert_to"])
        result = client.upload(
            content,
            metadata=meta,
            content_type=content_type,
            size=size,
            file_id=file_id,
            params={"fields": "id,name,mimeType,size,modifiedTime,webViewLink,parents"},
        )
        return PushResult(
            success=True,
            ref=result.get("id"),
            metadata={
                "file_id": result.get("id"),
                "name": result.get("name", name),
                "mime_type": result.get("mimeType", content_type),
                "size": int(result["size"]) if str(result.get("size") or "").isdigit() else size,
                "web_view_link": result.get("webViewLink", ""),
                "modified_time": result.get("modifiedTime", ""),
                "folder_id": folder,
                "conflict": action,
            },
        )

    def validate_config(self, config: dict) -> bool:
        """Offline shape check: a non-empty ``access_token`` (no network)."""
        return _token(config) is not None

    def max_size(self) -> int | None:
        return 5 * 1024**4  # Drive's 5 TB per-file limit


def _payload(envelope: DataEnvelope, filename: str) -> tuple[bytes | IO[bytes], int | None, str]:
    """``(content, size, content type)`` for an envelope's data or stream."""
    content_type = envelope.content_type or guess_mimetype(filename)
    if envelope.stream is not None:
        size = envelope.metadata.get("size") if isinstance(envelope.metadata, Mapping) else None
        known = size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None
        return envelope.stream, known, content_type
    data = envelope.data
    if isinstance(data, bytes):
        return data, len(data), content_type
    if isinstance(data, str):
        raw = data.encode("utf-8")
        return raw, len(raw), content_type
    if isinstance(data, dict):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        return raw, len(raw), content_type
    raise ValueError(f"unsupported envelope data type: {type(data).__name__}")


def _free_name(client: GoogleDriveRestClient, folder: str, filename: str) -> str:
    """``name (n).ext`` with the smallest n not already in the folder."""
    stem, ext = os.path.splitext(filename)
    q = (
        f"'{escape_query(folder)}' in parents and name contains '{escape_query(stem)}' "
        "and trashed = false"
    )
    taken = {str(f.get("name")) for f in _list_all(client, q, "name")}
    n = 1
    while f"{stem} ({n}){ext}" in taken:
        n += 1
    return f"{stem} ({n}){ext}"
