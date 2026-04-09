"""Local filesystem source and sink connectors for aisquare.pipe."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aisquare.pipe.core.connector import AuthType, SinkConnector, SourceConnector
from aisquare.pipe.core.envelope import (
    DataEnvelope,
    MetaField,
    PullParams,
    PushParams,
    PushResult,
    Resource,
)

from aisquare_pipe_local.client import LocalClient
from aisquare_pipe_local.constants import STREAM_THRESHOLD

logger = logging.getLogger("aisquare.pipe.local")


class LocalSource(SourceConnector):
    """Pull files from the local filesystem."""

    name = "local-source"
    version = "0.1.0"
    output_types = ["*/*"]
    auth_type = AuthType.NONE
    description = "Pull files from the local filesystem"
    docs_url = ""
    metadata_spec = {
        "filename": MetaField(type=str, required=True, description="Original filename"),
        "path": MetaField(
            type=str, required=True, description="Relative path from base_path"
        ),
        "size": MetaField(type=int, required=True, description="File size in bytes"),
        "modified_time": MetaField(
            type=str, required=False, description="ISO timestamp of last modification"
        ),
        "created_time": MetaField(
            type=str, required=False, description="ISO timestamp of creation"
        ),
        "permissions": MetaField(
            type=str, required=False, description="File permissions string"
        ),
    }

    def pull(
        self, config: dict, params: PullParams | None = None
    ) -> Iterator[DataEnvelope]:
        """Yield DataEnvelopes for each file under the configured base_path.

        Supported PullParams keys:
            path (str): Subdirectory within base_path (default: "")
            recursive (bool): Recurse into subdirectories (default: False)
            extensions (list[str]): Filter by extension, e.g. [".pdf", ".txt"]
            glob (str): Glob pattern to filter files, e.g. "*.csv"
            limit (int): Maximum number of files to pull
            stream_threshold (int): Byte size above which streaming is used
        """
        client = LocalClient(config)

        path = params.get("path", "") if params else ""
        recursive = params.get("recursive", False) if params else False
        extensions = params.get("extensions", None) if params else None
        glob_pattern = params.get("glob", None) if params else None
        limit = params.get("limit", None) if params else None
        stream_threshold = (
            params.get("stream_threshold", STREAM_THRESHOLD)
            if params
            else STREAM_THRESHOLD
        )

        # Choose iteration method
        if glob_pattern:
            base = client._resolve_safe(path)
            file_iter: Iterator[Path] = (
                p for p in sorted(base.glob(glob_pattern)) if p.is_file()
            )
        else:
            file_iter = client.list_files(path, recursive=recursive)

        count = 0
        for entry in file_iter:
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

            # Stat for metadata
            st = entry.stat()
            rel_path = str(entry.relative_to(client._base_path))

            metadata: dict[str, Any] = {
                "filename": entry.name,
                "path": rel_path,
                "size": st.st_size,
                "modified_time": datetime.fromtimestamp(
                    st.st_mtime, tz=timezone.utc
                ).isoformat(),
                "created_time": datetime.fromtimestamp(
                    st.st_ctime, tz=timezone.utc
                ).isoformat(),
                "permissions": oct(st.st_mode)[-3:],
            }

            # Stream large files, buffer small ones
            if st.st_size > stream_threshold:
                stream = client.read_stream(rel_path)
                yield DataEnvelope(
                    content_type=content_type,
                    data=b"",
                    source_id=self.name,
                    stream=stream,
                    metadata=metadata,
                )
            else:
                data = client.read_file(rel_path)
                yield DataEnvelope(
                    content_type=content_type,
                    data=data,
                    source_id=self.name,
                    metadata=metadata,
                )

            count += 1

    def validate_config(self, config: dict) -> bool:
        if not config.get("base_path"):
            return False
        try:
            return LocalClient(config).validate()
        except Exception:
            return False

    def list_resources(self, config: dict) -> list[Resource]:
        """List files and directories at the root of base_path."""
        client = LocalClient(config)
        resources: list[Resource] = []
        for entry in client.list_entries():
            rtype = "folder" if entry.is_dir() else "file"
            meta: dict[str, Any] = {"path": str(entry.relative_to(client._base_path))}
            if entry.is_file():
                meta["size"] = entry.stat().st_size
            resources.append(
                Resource(
                    id=str(entry.relative_to(client._base_path)),
                    name=entry.name,
                    resource_type=rtype,
                    metadata=meta,
                )
            )
        return resources

    def supports_streaming(self) -> bool:
        return True

    def rate_limit(self):
        return None


class LocalSink(SinkConnector):
    """Push files to the local filesystem."""

    name = "local-sink"
    version = "0.1.0"
    input_types = ["*/*"]
    auth_type = AuthType.NONE
    description = "Push files to the local filesystem"
    docs_url = ""
    metadata_spec = {
        "filename": MetaField(type=str, required=True, description="Target filename"),
        "path": MetaField(
            type=str,
            required=False,
            description="Target subdirectory within base_path",
            default="",
        ),
    }

    def push(
        self,
        envelope: DataEnvelope,
        config: dict,
        params: PushParams | None = None,
    ) -> PushResult:
        """Write an envelope's data to the local filesystem.

        Supported PushParams keys:
            target_path (str): Subdirectory within base_path (default: "")
            conflict (str): "fail", "overwrite", or "rename" (default: "fail")
            create_dirs (bool): Create parent directories (default: True)
            preserve_paths (bool): Use source relative path to recreate directory
                structure (default: True). Set False to flatten all files.
        """
        try:
            client = LocalClient(config)

            # Target path — use metadata["path"] to preserve directory structure,
            # fall back to just filename for flat output
            target_folder = params.get("target_path", "") if params else ""
            preserve = params.get("preserve_paths", True) if params else True
            if preserve and "path" in envelope.metadata:
                # Strip leading slashes — cloud sources use absolute paths like /Documents/file.pdf
                rel_path = envelope.metadata["path"].lstrip("/")
            else:
                rel_path = envelope.metadata.get("filename", "unnamed_file")
            full_path = f"{target_folder}/{rel_path}" if target_folder else rel_path

            # Conflict mode
            conflict = params.get("conflict", "fail") if params else "fail"
            create_dirs = params.get("create_dirs", True) if params else True

            # Write based on data type
            if envelope.stream is not None:
                result_meta = client.write_stream(
                    envelope.stream,
                    full_path,
                    conflict=conflict,
                    create_dirs=create_dirs,
                )
            elif isinstance(envelope.data, bytes):
                result_meta = client.write_file(
                    envelope.data,
                    full_path,
                    conflict=conflict,
                    create_dirs=create_dirs,
                )
            elif isinstance(envelope.data, str):
                data_bytes = envelope.data.encode("utf-8")
                result_meta = client.write_file(
                    data_bytes,
                    full_path,
                    conflict=conflict,
                    create_dirs=create_dirs,
                )
            elif isinstance(envelope.data, dict):
                data_bytes = json.dumps(
                    envelope.data, ensure_ascii=False
                ).encode("utf-8")
                result_meta = client.write_file(
                    data_bytes,
                    full_path,
                    conflict=conflict,
                    create_dirs=create_dirs,
                )
            else:
                return PushResult(
                    success=False,
                    error=f"Unsupported data type: {type(envelope.data).__name__}",
                )

            return PushResult(
                success=True,
                ref=result_meta["path"],
                metadata=result_meta,
            )

        except Exception as e:
            logger.error("Local push failed: %s", e)
            return PushResult(success=False, error=str(e))

    def validate_config(self, config: dict) -> bool:
        if not config.get("base_path"):
            return False
        try:
            return LocalClient(config).validate(writable=True)
        except Exception:
            return False

    def max_size(self) -> int | None:
        return None
