"""Google Drive source and sink connectors for aisquare.pipe."""

from __future__ import annotations

import fnmatch
import io
import json
import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from aisquare.pipe.core.connector import AuthType, SinkConnector, SourceConnector
from aisquare.pipe.core.envelope import (
    DataEnvelope,
    MetaField,
    PullParams,
    PushParams,
    PushResult,
    RateLimit,
)
from aisquare_pipe_gdrive.client import GDriveClient
from aisquare_pipe_gdrive.constants import (
    DEFAULT_EXPORT_FORMATS,
    EXPORT_EXTENSIONS,
    FOLDER_MIME,
    NATIVE_MIME_PREFIX,
    NATIVE_SHORT_NAMES,
    STREAM_THRESHOLD,
)

logger = logging.getLogger("aisquare.pipe.gdrive")


def _has_valid_auth_keys(config: dict[str, Any]) -> bool:
    """Check whether config carries a usable credential set.

    Offline and total — never raises, never touches the network. This is what
    keeps validate_config({}) honest under the compliance socket guard.
    """
    if not isinstance(config, dict):
        return False
    if config.get("access_token"):
        return True
    if config.get("service_account_json"):
        return True
    return all(config.get(k) for k in ("client_id", "client_secret", "refresh_token"))


def _resolve_export_formats(overrides: dict[str, str] | None) -> dict[str, str]:
    """Merge user overrides into the default native-MIME -> target-MIME map.

    Overrides are keyed by short name ("document", "spreadsheet", ...) for config
    readability; the returned map is keyed by full native MIME type.
    """
    formats = dict(DEFAULT_EXPORT_FORMATS)
    for short_name, target_mime in (overrides or {}).items():
        native_mime = NATIVE_SHORT_NAMES.get(short_name, short_name)
        formats[native_mime] = target_mime
    return formats


class GDriveSource(SourceConnector):
    """Pull files from Google Drive via the Drive v3 API."""

    name = "gdrive-source"
    version = "0.1.0"
    output_types = ["*/*"]  # noqa: RUF012 — matches the base class's own plain attribute
    auth_type = AuthType.OAUTH2
    description = "Pull files from Google Drive"
    docs_url = "https://developers.google.com/drive/api/v3/reference"
    metadata_spec = {  # noqa: RUF012 — matches the base class's own plain attribute
        "filename": MetaField(type=str, required=True, description="Original filename"),
        "file_id": MetaField(type=str, required=True, description="Drive file ID"),
        "mime_type": MetaField(
            type=str, required=True, description="MIME type as reported by Drive"
        ),
        "size": MetaField(type=int, required=True, description="File size in bytes"),
        "modified_time": MetaField(
            type=str, required=False, description="ISO timestamp of last modification"
        ),
        "web_view_link": MetaField(
            type=str, required=False, description="Browser URL for the file"
        ),
        "md5_checksum": MetaField(
            type=str, required=False, description="MD5 of the content, when Drive provides one"
        ),
        "parents": MetaField(
            type=list, required=False, description="Parent folder IDs"
        ),
        "exported_from": MetaField(
            type=str,
            required=False,
            description="Original Google-native MIME type, set only on exported files",
        ),
    }

    def pull(
        self, config: dict, params: PullParams | None = None
    ) -> Iterator[DataEnvelope]:
        """Yield a DataEnvelope for each file under the configured folder.

        Supported PullParams keys:
            folder_id (str): Folder to list (default: "root")
            recursive (bool): Descend into subfolders (default: False)
            query (str): Raw Drive query, ANDed with the parent/trashed clauses
            extensions (list[str]): Filter by extension, e.g. [".pdf", ".docx"]
            glob (str): Glob pattern matched against the filename
            limit (int): Maximum number of files to emit
            stream_threshold (int): Byte size above which streaming is used
        """
        client = GDriveClient(config)

        folder_id = params.get("folder_id", "root") if params else "root"
        recursive = params.get("recursive", False) if params else False
        query = params.get("query", "") if params else ""
        extensions = params.get("extensions", None) if params else None
        glob_pattern = params.get("glob", None) if params else None
        limit = params.get("limit", None) if params else None
        stream_threshold = (
            params.get("stream_threshold", STREAM_THRESHOLD)
            if params
            else STREAM_THRESHOLD
        )
        export_formats = _resolve_export_formats(
            params.get("export_formats", None) if params else None
        )
        skip_native = params.get("skip_google_native", False) if params else False

        count = 0
        for item in client.list_files(
            folder_id=folder_id, recursive=recursive, query=query
        ):
            if item.get("mimeType") == FOLDER_MIME:
                continue

            name = item.get("name", "")

            if extensions:
                ext = os.path.splitext(name)[1].lower()
                if ext not in extensions:
                    continue

            if glob_pattern and not fnmatch.fnmatch(name, glob_pattern):
                continue

            if limit is not None and count >= limit:
                return

            envelope = self._build_envelope(
                client, item, stream_threshold, export_formats, skip_native
            )
            if envelope is None:
                continue

            yield envelope
            count += 1

    def _build_envelope(
        self,
        client: GDriveClient,
        item: dict[str, Any],
        stream_threshold: int,
        export_formats: dict[str, str],
        skip_native: bool,
    ) -> DataEnvelope | None:
        """Turn one Drive file resource into an envelope, or None to skip it."""
        mime_type = item.get("mimeType", "application/octet-stream")

        if mime_type.startswith(NATIVE_MIME_PREFIX):
            return self._build_exported_envelope(
                client, item, mime_type, export_formats, skip_native
            )

        size = int(item.get("size") or 0)
        metadata = self._base_metadata(item, size)

        if size > stream_threshold:
            return DataEnvelope(
                content_type=mime_type,
                data=b"",
                source_id=self.name,
                stream=client.download_stream(item["id"]),
                metadata=metadata,
            )

        return DataEnvelope(
            content_type=mime_type,
            data=client.download(item["id"]),
            source_id=self.name,
            metadata=metadata,
        )

    def _build_exported_envelope(
        self,
        client: GDriveClient,
        item: dict[str, Any],
        native_mime: str,
        export_formats: dict[str, str],
        skip_native: bool,
    ) -> DataEnvelope | None:
        """Export a Google-native file, or return None when it cannot be exported.

        Native files carry no bytes and report no size, so files.get?alt=media
        fails on them. Forms, Apps Script and Shortcuts have no export path at
        all — those are logged and skipped rather than failing the whole run.
        """
        if skip_native:
            logger.debug("gdrive: skipping native file %r", item.get("name"))
            return None

        target_mime = export_formats.get(native_mime)
        if not target_mime:
            logger.warning(
                "gdrive: no export format for %r (%s) — skipping",
                item.get("name"),
                native_mime,
            )
            return None

        data = client.export(item["id"], target_mime)

        metadata = self._base_metadata(item, len(data))
        metadata["exported_from"] = native_mime
        metadata["mime_type"] = target_mime
        metadata["filename"] = self._with_extension(item.get("name", ""), target_mime)

        return DataEnvelope(
            content_type=target_mime,
            data=data,
            source_id=self.name,
            metadata=metadata,
        )

    @staticmethod
    def _with_extension(name: str, target_mime: str) -> str:
        """Append the export target's extension unless the name already has it."""
        ext = EXPORT_EXTENSIONS.get(target_mime, "")
        if not ext or name.lower().endswith(ext):
            return name
        return f"{name}{ext}"

    @staticmethod
    def _base_metadata(item: dict[str, Any], size: int) -> dict[str, Any]:
        """Metadata common to every emitted envelope."""
        metadata: dict[str, Any] = {
            "filename": item.get("name", ""),
            "file_id": item.get("id", ""),
            "mime_type": item.get("mimeType", ""),
            "size": size,
        }
        for src_key, dst_key in (
            ("modifiedTime", "modified_time"),
            ("webViewLink", "web_view_link"),
            ("md5Checksum", "md5_checksum"),
            ("parents", "parents"),
        ):
            if item.get(src_key):
                metadata[dst_key] = item[src_key]
        return metadata

    def validate_config(self, config: dict) -> bool:
        return _has_valid_auth_keys(config)

    def supports_streaming(self) -> bool:
        return True

    def rate_limit(self) -> RateLimit | None:
        # Drive's default quota is 12,000 queries per 60 seconds per project.
        return RateLimit(requests_per_minute=12000, concurrent=1)


@dataclass
class _Resolved:
    """Outcome of applying the sink's conflict policy to a target filename."""

    name: str
    existing_id: str = ""
    error: str = ""


class GDriveSink(SinkConnector):
    """Push files to Google Drive via the Drive v3 API."""

    name = "gdrive-sink"
    version = "0.1.0"
    input_types = ["*/*"]  # noqa: RUF012 — matches the base class's own plain attribute
    auth_type = AuthType.OAUTH2
    description = "Push files to Google Drive"
    docs_url = "https://developers.google.com/drive/api/v3/reference"
    metadata_spec = {  # noqa: RUF012 — matches the base class's own plain attribute
        "filename": MetaField(type=str, required=True, description="Target filename"),
    }

    def push(
        self,
        envelope: DataEnvelope,
        config: dict,
        params: PushParams | None = None,
    ) -> PushResult:
        """Upload an envelope's payload to Google Drive.

        Supported PushParams keys:
            folder_id (str): Destination folder ID (default: "root")
            target_path (str): Slash-separated subfolder path, created as needed
            conflict (str): "fail" (default), "overwrite", or "rename"

        Note: A stream envelope pushed with conflict="overwrite" is buffered fully
        into memory, because Drive's update-in-place API takes bytes rather than
        a streaming session. For very large files, prefer "rename" or "fail" modes.
        A stream envelope whose metadata declares no 'size' is likewise buffered,
        since a resumable session cannot be opened without a total byte count.

        Never raises: every failure comes back as PushResult(success=False).
        """
        try:
            filename = envelope.metadata.get("filename")
            if not filename:
                return PushResult(
                    success=False,
                    error="envelope metadata is missing 'filename'",
                )

            client = GDriveClient(config)

            folder_id = params.get("folder_id", "root") if params else "root"
            target_path = params.get("target_path", "") if params else ""
            conflict = params.get("conflict", "fail") if params else "fail"

            parent_id = (
                client.ensure_folder_path(target_path, folder_id)
                if target_path
                else folder_id
            )

            mime_type = envelope.content_type or "application/octet-stream"

            # A resumable session needs the total size up front for its
            # Content-Range headers. Stream envelopes normally declare one in
            # metadata; when they do not, buffer the stream and let the sized
            # byte path below handle it, rather than opening a session that can
            # never be completed.
            if envelope.stream is not None:
                size = int(envelope.metadata.get("size") or 0)
                if size > 0:
                    resolved = self._resolve_conflict(
                        client, filename, parent_id, conflict
                    )
                    if resolved.error:
                        return PushResult(success=False, error=resolved.error)
                    if resolved.existing_id and conflict == "overwrite":
                        # Note: buffers the entire stream into memory.
                        result = client.update_content(
                            resolved.existing_id, mime_type, envelope.stream.read()
                        )
                    else:
                        result = client.upload_resumable(
                            resolved.name, parent_id, mime_type, envelope.stream, size
                        )
                    return self._success(result)
                data: bytes | None = envelope.stream.read()
            else:
                data = self._coerce(envelope.data)

            if data is None:
                return PushResult(
                    success=False,
                    error=f"Unsupported data type: {type(envelope.data).__name__}",
                )

            resolved = self._resolve_conflict(client, filename, parent_id, conflict)
            if resolved.error:
                return PushResult(success=False, error=resolved.error)

            if resolved.existing_id and conflict == "overwrite":
                result = client.update_content(resolved.existing_id, mime_type, data)
            elif client.should_chunk(len(data)):
                result = client.upload_resumable(
                    resolved.name, parent_id, mime_type, io.BytesIO(data), len(data)
                )
            else:
                result = client.upload(resolved.name, parent_id, mime_type, data)

            return self._success(result)

        except Exception as e:  # noqa: BLE001 — push must never raise
            logger.error("gdrive push failed: %s", e)
            return PushResult(success=False, error=str(e))

    @staticmethod
    def _coerce(data: Any) -> bytes | None:
        """Normalize envelope payloads to bytes. None means unsupported."""
        if isinstance(data, bytes):
            return data
        if isinstance(data, str):
            return data.encode("utf-8")
        if isinstance(data, dict):
            return json.dumps(data, ensure_ascii=False).encode("utf-8")
        return None

    def _resolve_conflict(
        self, client: GDriveClient, filename: str, parent_id: str, conflict: str
    ) -> _Resolved:
        """Apply the conflict policy, returning the name to write under.

        Drive permits duplicate names in a folder, so the check is explicit
        rather than delegated to the API.
        """
        existing = client.find_child(filename, parent_id)
        if not existing:
            return _Resolved(name=filename)

        if conflict == "overwrite":
            return _Resolved(name=filename, existing_id=existing["id"])

        if conflict == "rename":
            stem, ext = os.path.splitext(filename)
            for n in range(1, 1000):
                candidate = f"{stem} ({n}){ext}"
                if client.find_child(candidate, parent_id) is None:
                    return _Resolved(name=candidate)
            return _Resolved(
                name=filename,
                error=f"could not find a free name for {filename!r} after 999 tries",
            )

        return _Resolved(
            name=filename,
            error=(
                f"{filename!r} already exists in the target folder "
                "(set conflict to 'overwrite' or 'rename')"
            ),
        )

    @staticmethod
    def _success(result: dict[str, Any]) -> PushResult:
        """Build a PushResult from a Drive file resource."""
        return PushResult(
            success=True,
            ref=result.get("id"),
            metadata={
                "file_id": result.get("id", ""),
                "filename": result.get("name", ""),
                "web_view_link": result.get("webViewLink", ""),
            },
        )

    def validate_config(self, config: dict) -> bool:
        return _has_valid_auth_keys(config)

    def max_size(self) -> int | None:
        # Drive's per-file ceiling is 5 TB; no practical guard is useful here.
        return None
