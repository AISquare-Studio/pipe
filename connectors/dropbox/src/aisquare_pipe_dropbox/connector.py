"""Dropbox source and sink connectors for aisquare.pipe."""

from __future__ import annotations

import io
import json
import logging
import mimetypes
import os
from collections.abc import Iterator
from typing import Any

import dropbox.files

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

from aisquare_pipe_dropbox.client import DropboxClient
from aisquare_pipe_dropbox.constants import STREAM_THRESHOLD

logger = logging.getLogger("aisquare.pipe.dropbox")


def _has_valid_auth_keys(config: dict[str, Any]) -> bool:
    """Check whether config has either access_token or refresh token set."""
    if "access_token" in config:
        return True
    return all(k in config for k in ("app_key", "app_secret", "refresh_token"))


class DropboxSource(SourceConnector):
    """Pull files from Dropbox."""

    name = "dropbox-source"
    version = "0.1.0"
    output_types = ["*/*"]
    auth_type = AuthType.OAUTH2
    description = "Pull files from Dropbox"
    docs_url = "https://www.dropbox.com/developers/documentation"
    metadata_spec = {
        "filename": MetaField(type=str, required=True, description="Original filename"),
        "path": MetaField(type=str, required=True, description="Full Dropbox path"),
        "size": MetaField(type=int, required=True, description="File size in bytes"),
        "rev": MetaField(type=str, required=False, description="Dropbox revision ID"),
        "server_modified": MetaField(
            type=str, required=False, description="ISO timestamp of last modification"
        ),
        "dropbox_id": MetaField(type=str, required=False, description="Dropbox file ID"),
    }

    def pull(
        self, config: dict, params: PullParams | None = None
    ) -> Iterator[DataEnvelope]:
        """Yield DataEnvelopes for each file in the configured Dropbox folder.

        Supported PullParams keys:
            path (str): Folder path to list (default: root "")
            recursive (bool): Recurse into subfolders (default: False)
            extensions (list[str]): Filter by extension, e.g. [".pdf", ".docx"]
            limit (int): Maximum number of files to pull
            stream_threshold (int): Byte size above which streaming is used
        """
        client = DropboxClient(config)

        path = params.get("path", "") if params else ""
        recursive = params.get("recursive", False) if params else False
        extensions = params.get("extensions", None) if params else None
        limit = params.get("limit", None) if params else None
        stream_threshold = (
            params.get("stream_threshold", STREAM_THRESHOLD) if params else STREAM_THRESHOLD
        )

        count = 0
        for entry in client.list_folder(path, recursive=recursive):
            # Skip folders — only yield files
            if not isinstance(entry, dropbox.files.FileMetadata):
                continue

            # Extension filter
            if extensions:
                ext = os.path.splitext(entry.name)[1].lower()
                if ext not in extensions:
                    continue

            # Limit
            if limit is not None and count >= limit:
                return

            # MIME type from filename
            mime_type, _ = mimetypes.guess_type(entry.name)
            content_type = mime_type or "application/octet-stream"

            metadata = {
                "filename": entry.name,
                "path": entry.path_lower,
                "size": entry.size,
                "rev": entry.rev,
                "server_modified": entry.server_modified.isoformat(),
                "dropbox_id": entry.id,
            }

            # Stream large files, buffer small ones
            if entry.size > stream_threshold:
                _, stream = client.download_stream(entry.path_lower)
                yield DataEnvelope(
                    content_type=content_type,
                    data=b"",
                    source_id=self.name,
                    stream=stream,
                    metadata=metadata,
                )
            else:
                _, content = client.download(entry.path_lower)
                yield DataEnvelope(
                    content_type=content_type,
                    data=content,
                    source_id=self.name,
                    metadata=metadata,
                )

            count += 1

    def validate_config(self, config: dict) -> bool:
        if not _has_valid_auth_keys(config):
            return False
        try:
            return DropboxClient(config).validate()
        except Exception:
            return False

    def list_resources(self, config: dict) -> list[Resource]:
        """Browse the root of the connected Dropbox."""
        client = DropboxClient(config)
        resources: list[Resource] = []
        for entry in client.list_folder("", recursive=False):
            if isinstance(entry, dropbox.files.FolderMetadata):
                rtype = "folder"
                meta: dict[str, Any] = {"path": entry.path_lower}
            else:
                rtype = "file"
                fm = entry  # type: ignore[assignment]
                meta = {"path": fm.path_lower, "size": fm.size}
            resources.append(
                Resource(
                    id=entry.id if hasattr(entry, "id") else entry.path_lower,
                    name=entry.name,
                    resource_type=rtype,
                    metadata=meta,
                )
            )
        return resources

    def supports_streaming(self) -> bool:
        return True

    def rate_limit(self) -> RateLimit | None:
        return RateLimit(requests_per_minute=600, concurrent=1)


class DropboxSink(SinkConnector):
    """Push files to Dropbox."""

    name = "dropbox-sink"
    version = "0.1.0"
    input_types = ["*/*"]
    auth_type = AuthType.OAUTH2
    description = "Push files to Dropbox"
    docs_url = "https://www.dropbox.com/developers/documentation"
    metadata_spec = {
        "filename": MetaField(type=str, required=True, description="Target filename"),
        "path": MetaField(
            type=str,
            required=False,
            description="Target folder path in Dropbox",
            default="/",
        ),
    }

    def push(
        self,
        envelope: DataEnvelope,
        config: dict,
        params: PushParams | None = None,
    ) -> PushResult:
        """Upload an envelope's data to Dropbox.

        Supported PushParams keys:
            target_path (str): Destination folder (default: "/")
            overwrite (bool): Overwrite existing files (default: False)
        """
        try:
            client = DropboxClient(config)

            # Target path
            target_folder = params.get("target_path", "/") if params else "/"
            filename = envelope.metadata.get("filename", "unnamed_file")
            full_path = f"{target_folder.rstrip('/')}/{filename}"

            # Write mode
            overwrite = params.get("overwrite", False) if params else False
            mode = (
                dropbox.files.WriteMode.overwrite
                if overwrite
                else dropbox.files.WriteMode.add
            )

            # Resolve data to uploadable form
            if envelope.stream is not None:
                size = envelope.metadata.get("size", 0)
                result_meta = client.upload_chunked(envelope.stream, full_path, size, mode)
            elif isinstance(envelope.data, bytes):
                if client.should_chunk(len(envelope.data)):
                    stream = io.BytesIO(envelope.data)
                    result_meta = client.upload_chunked(
                        stream, full_path, len(envelope.data), mode
                    )
                else:
                    result_meta = client.upload(envelope.data, full_path, mode)
            elif isinstance(envelope.data, str):
                data_bytes = envelope.data.encode("utf-8")
                result_meta = client.upload(data_bytes, full_path, mode)
            elif isinstance(envelope.data, dict):
                data_bytes = json.dumps(envelope.data, ensure_ascii=False).encode("utf-8")
                result_meta = client.upload(data_bytes, full_path, mode)
            else:
                return PushResult(
                    success=False,
                    error=f"Unsupported data type: {type(envelope.data).__name__}",
                )

            return PushResult(
                success=True,
                ref=result_meta.id,
                metadata={
                    "path": result_meta.path_lower,
                    "rev": result_meta.rev,
                    "size": result_meta.size,
                    "server_modified": str(result_meta.server_modified),
                },
            )

        except Exception as e:
            logger.error("Dropbox push failed: %s", e)
            return PushResult(success=False, error=str(e))

    def validate_config(self, config: dict) -> bool:
        if not _has_valid_auth_keys(config):
            return False
        try:
            return DropboxClient(config).validate()
        except Exception:
            return False

    def max_size(self) -> int | None:
        return 350 * 1024 * 1024 * 1024  # 350 GB via upload sessions
