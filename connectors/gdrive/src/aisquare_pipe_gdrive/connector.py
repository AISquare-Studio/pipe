"""Google Drive source and sink connectors for aisquare.pipe."""

from __future__ import annotations

import fnmatch
import logging
import os
from collections.abc import Iterator
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
    output_types = ["*/*"]
    auth_type = AuthType.OAUTH2
    description = "Pull files from Google Drive"
    docs_url = "https://developers.google.com/drive/api/v3/reference"
    metadata_spec = {
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


class GDriveSink(SinkConnector):
    """Push files to Google Drive via the Drive v3 API."""

    name = "gdrive-sink"
    version = "0.1.0"
    input_types = ["*/*"]
    auth_type = AuthType.OAUTH2
    description = "Push files to Google Drive"
    docs_url = "https://developers.google.com/drive/api/v3/reference"
    metadata_spec = {
        "filename": MetaField(type=str, required=True, description="Target filename"),
        "size": MetaField(
            type=int, required=False, description="Byte size, used to pick the upload path"
        ),
    }

    def push(
        self,
        envelope: DataEnvelope,
        config: dict,
        params: PushParams | None = None,
    ) -> PushResult:
        if not _has_valid_auth_keys(config):
            return PushResult(success=False, error="gdrive config is missing credentials")
        return PushResult(success=False, error="not implemented")

    def validate_config(self, config: dict) -> bool:
        return _has_valid_auth_keys(config)

    def max_size(self) -> int | None:
        # Drive's per-file ceiling is 5 TB; no practical guard is useful here.
        return None
