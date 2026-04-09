"""OneDrive source and sink connectors for aisquare.pipe."""

from __future__ import annotations

import io
import json
import logging
import mimetypes
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
    Resource,
)

from aisquare_pipe_onedrive.client import OneDriveClient
from aisquare_pipe_onedrive.constants import STREAM_THRESHOLD

logger = logging.getLogger("aisquare.pipe.onedrive")


def _has_valid_auth_keys(config: dict[str, Any]) -> bool:
    """Check whether config has valid auth credentials."""
    if "access_token" in config:
        return True
    return all(k in config for k in ("client_id", "client_secret", "tenant_id"))


class OneDriveSource(SourceConnector):
    """Pull files from OneDrive via Microsoft Graph API."""

    name = "onedrive-source"
    version = "0.1.0"
    output_types = ["*/*"]
    auth_type = AuthType.OAUTH2
    description = "Pull files from Microsoft OneDrive"
    docs_url = "https://learn.microsoft.com/en-us/graph/api/resources/onedrive"
    metadata_spec = {
        "filename": MetaField(type=str, required=True, description="Original filename"),
        "path": MetaField(type=str, required=True, description="Full OneDrive path"),
        "size": MetaField(type=int, required=True, description="File size in bytes"),
        "item_id": MetaField(type=str, required=False, description="OneDrive item ID"),
        "last_modified": MetaField(
            type=str, required=False, description="ISO timestamp of last modification"
        ),
        "web_url": MetaField(type=str, required=False, description="Web URL to file"),
        "mime_type": MetaField(type=str, required=False, description="MIME type from OneDrive"),
    }

    def pull(
        self, config: dict, params: PullParams | None = None
    ) -> Iterator[DataEnvelope]:
        """Yield DataEnvelopes for each file in the configured OneDrive folder.

        Supported PullParams keys:
            path (str): Folder path to list (default: root "")
            recursive (bool): Recurse into subfolders (default: False)
            extensions (list[str]): Filter by extension, e.g. [".pdf", ".docx"]
            limit (int): Maximum number of files to pull
            stream_threshold (int): Byte size above which streaming is used
        """
        client = OneDriveClient(config)

        path = params.get("path", "") if params else ""
        recursive = params.get("recursive", False) if params else False
        extensions = params.get("extensions", None) if params else None
        limit = params.get("limit", None) if params else None
        stream_threshold = (
            params.get("stream_threshold", STREAM_THRESHOLD) if params else STREAM_THRESHOLD
        )

        count = 0
        for item in client.list_folder(path, recursive=recursive):
            # Skip folders — only yield files
            if "file" not in item:
                continue

            name = item["name"]

            # Extension filter
            if extensions:
                ext = os.path.splitext(name)[1].lower()
                if ext not in extensions:
                    continue

            # Limit
            if limit is not None and count >= limit:
                return

            # MIME type from OneDrive metadata (unlike Dropbox, Graph API provides it)
            content_type = item.get("file", {}).get("mimeType")
            if not content_type:
                guessed, _ = mimetypes.guess_type(name)
                content_type = guessed or "application/octet-stream"

            item_id = item["id"]
            size = item.get("size", 0)
            parent_path = item.get("parentReference", {}).get("path", "")
            item_path = f"{parent_path}/{name}" if parent_path else name

            metadata = {
                "filename": name,
                "path": item_path,
                "size": size,
                "item_id": item_id,
                "last_modified": item.get("lastModifiedDateTime", ""),
                "web_url": item.get("webUrl", ""),
                "mime_type": content_type,
            }

            # Stream large files, buffer small ones
            if size > stream_threshold:
                _, stream = client.download_stream(item_id)
                yield DataEnvelope(
                    content_type=content_type,
                    data=b"",
                    source_id=self.name,
                    stream=stream,
                    metadata=metadata,
                )
            else:
                _, content = client.download(item_id)
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
            return OneDriveClient(config).validate()
        except Exception:
            return False

    def list_resources(self, config: dict) -> list[Resource]:
        """Browse the root of the connected OneDrive."""
        client = OneDriveClient(config)
        resources: list[Resource] = []
        for item in client.list_folder("", recursive=False):
            rtype = "folder" if "folder" in item else "file"
            meta: dict[str, Any] = {"path": item.get("name", "")}
            if rtype == "file":
                meta["size"] = item.get("size", 0)
                meta["mime_type"] = item.get("file", {}).get("mimeType", "")
            resources.append(
                Resource(
                    id=item.get("id", ""),
                    name=item.get("name", ""),
                    resource_type=rtype,
                    metadata=meta,
                )
            )
        return resources

    def supports_streaming(self) -> bool:
        return True

    def rate_limit(self) -> RateLimit | None:
        return RateLimit(requests_per_minute=600, concurrent=1)


class OneDriveSink(SinkConnector):
    """Push files to OneDrive via Microsoft Graph API."""

    name = "onedrive-sink"
    version = "0.1.0"
    input_types = ["*/*"]
    auth_type = AuthType.OAUTH2
    description = "Push files to Microsoft OneDrive"
    docs_url = "https://learn.microsoft.com/en-us/graph/api/resources/onedrive"
    metadata_spec = {
        "filename": MetaField(type=str, required=True, description="Target filename"),
        "path": MetaField(
            type=str,
            required=False,
            description="Target folder path in OneDrive",
            default="/",
        ),
    }

    def push(
        self,
        envelope: DataEnvelope,
        config: dict,
        params: PushParams | None = None,
    ) -> PushResult:
        """Upload an envelope's data to OneDrive.

        Supported PushParams keys:
            target_path (str): Destination folder (default: "/")
            conflict (str): Conflict behavior — "rename", "replace", "fail" (default: "rename")
        """
        try:
            client = OneDriveClient(config)

            # Target path
            target_folder = params.get("target_path", "/") if params else "/"
            filename = envelope.metadata.get("filename", "unnamed_file")
            full_path = f"{target_folder.strip('/')}/{filename}".strip("/")

            # Conflict behavior
            conflict = params.get("conflict", "rename") if params else "rename"

            # Resolve data to uploadable form
            if envelope.stream is not None:
                size = envelope.metadata.get("size", 0)
                result_meta = client.upload_chunked(
                    envelope.stream, full_path, size, conflict
                )
            elif isinstance(envelope.data, bytes):
                if client.should_chunk(len(envelope.data)):
                    stream = io.BytesIO(envelope.data)
                    result_meta = client.upload_chunked(
                        stream, full_path, len(envelope.data), conflict
                    )
                else:
                    result_meta = client.upload(envelope.data, full_path, conflict)
            elif isinstance(envelope.data, str):
                data_bytes = envelope.data.encode("utf-8")
                if client.should_chunk(len(data_bytes)):
                    stream = io.BytesIO(data_bytes)
                    result_meta = client.upload_chunked(
                        stream, full_path, len(data_bytes), conflict
                    )
                else:
                    result_meta = client.upload(data_bytes, full_path, conflict)
            elif isinstance(envelope.data, dict):
                data_bytes = json.dumps(envelope.data, ensure_ascii=False).encode("utf-8")
                result_meta = client.upload(data_bytes, full_path, conflict)
            else:
                return PushResult(
                    success=False,
                    error=f"Unsupported data type: {type(envelope.data).__name__}",
                )

            return PushResult(
                success=True,
                ref=result_meta.get("id"),
                metadata={
                    "path": result_meta.get("name", ""),
                    "size": result_meta.get("size", 0),
                    "web_url": result_meta.get("webUrl", ""),
                    "last_modified": result_meta.get("lastModifiedDateTime", ""),
                },
            )

        except Exception as e:
            logger.error("OneDrive push failed: %s", e)
            return PushResult(success=False, error=str(e))

    def validate_config(self, config: dict) -> bool:
        if not _has_valid_auth_keys(config):
            return False
        try:
            return OneDriveClient(config).validate()
        except Exception:
            return False

    def max_size(self) -> int | None:
        return 250 * 1024 * 1024 * 1024  # 250 GB via resumable upload sessions
