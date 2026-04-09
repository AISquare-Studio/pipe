"""Shared Dropbox SDK wrapper used by both source and sink connectors."""

from __future__ import annotations

import functools
import io
import logging
import time
from collections.abc import Iterator
from typing import IO, Any

import dropbox
import dropbox.exceptions
import dropbox.files

from aisquare.pipe.errors import ConfigValidationError, PipelineError

from aisquare_pipe_dropbox.constants import (
    CHUNK_SIZE,
    CHUNK_THRESHOLD,
    INITIAL_BACKOFF,
    MAX_RETRIES,
)

logger = logging.getLogger("aisquare.pipe.dropbox")


def _map_dropbox_errors(func):  # type: ignore[type-arg]
    """Decorator that translates Dropbox SDK errors to framework errors."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except dropbox.exceptions.AuthError as e:
            raise ConfigValidationError(f"Dropbox auth failed: {e}") from e
        except dropbox.exceptions.ApiError as e:
            raise PipelineError(f"Dropbox API error: {e}") from e

    return wrapper


def _retry_on_rate_limit(func):  # type: ignore[type-arg]
    """Decorator that retries on 429 (rate limited) with exponential backoff."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        for attempt in range(MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except dropbox.exceptions.HttpError as e:
                if e.status_code == 429:
                    wait = INITIAL_BACKOFF * (2**attempt)
                    logger.warning(
                        "Rate limited by Dropbox (attempt %d/%d), "
                        "retrying in %.1fs",
                        attempt + 1,
                        MAX_RETRIES,
                        wait,
                    )
                    time.sleep(wait)
                else:
                    raise
        raise PipelineError("Dropbox rate limit exceeded after max retries")

    return wrapper


class DropboxClient:
    """Wraps the Dropbox SDK with auth, pagination, chunked upload, and error handling."""

    def __init__(self, config: dict[str, Any]) -> None:
        if "access_token" in config:
            self._dbx = dropbox.Dropbox(oauth2_access_token=config["access_token"])
        elif all(k in config for k in ("app_key", "app_secret", "refresh_token")):
            self._dbx = dropbox.Dropbox(
                app_key=config["app_key"],
                app_secret=config["app_secret"],
                oauth2_refresh_token=config["refresh_token"],
            )
        else:
            raise ConfigValidationError(
                "Dropbox config requires either 'access_token' or "
                "all of 'app_key', 'app_secret', 'refresh_token'"
            )

    @_retry_on_rate_limit
    @_map_dropbox_errors
    def validate(self) -> bool:
        """Test credentials by fetching the current account."""
        self._dbx.users_get_current_account()
        return True

    def list_folder(
        self, path: str = "", recursive: bool = False
    ) -> Iterator[dropbox.files.FileMetadata | dropbox.files.FolderMetadata]:
        """List folder contents with automatic pagination.

        Yields FileMetadata and FolderMetadata entries.
        """
        try:
            result = self._dbx.files_list_folder(path, recursive=recursive)
            yield from result.entries

            while result.has_more:
                result = self._dbx.files_list_folder_continue(result.cursor)
                yield from result.entries
        except dropbox.exceptions.AuthError as e:
            raise ConfigValidationError(f"Dropbox auth failed: {e}") from e
        except dropbox.exceptions.ApiError as e:
            raise PipelineError(f"Dropbox API error: {e}") from e

    @_retry_on_rate_limit
    @_map_dropbox_errors
    def download(self, path: str) -> tuple[dropbox.files.FileMetadata, bytes]:
        """Download a file and return (metadata, content_bytes)."""
        metadata, response = self._dbx.files_download(path)
        try:
            content = response.content
        finally:
            response.close()
        return metadata, content

    @_retry_on_rate_limit
    @_map_dropbox_errors
    def download_stream(
        self, path: str
    ) -> tuple[dropbox.files.FileMetadata, IO[bytes]]:
        """Download a file and return (metadata, stream).

        Caller is responsible for closing the stream.
        """
        metadata, response = self._dbx.files_download(path)
        return metadata, io.BytesIO(response.content)

    @_retry_on_rate_limit
    @_map_dropbox_errors
    def upload(
        self,
        data: bytes,
        path: str,
        mode: dropbox.files.WriteMode | None = None,
    ) -> dropbox.files.FileMetadata:
        """Upload a file (must be < 150MB). Returns FileMetadata."""
        if mode is None:
            mode = dropbox.files.WriteMode.add
        return self._dbx.files_upload(data, path, mode=mode)

    @_retry_on_rate_limit
    @_map_dropbox_errors
    def upload_chunked(
        self,
        stream: IO[bytes],
        path: str,
        size: int,
        mode: dropbox.files.WriteMode | None = None,
    ) -> dropbox.files.FileMetadata:
        """Upload a large file using upload sessions (for files >= 140MB).

        Reads from stream in CHUNK_SIZE increments.
        """
        if mode is None:
            mode = dropbox.files.WriteMode.add

        # Start session
        chunk = stream.read(CHUNK_SIZE)
        session = self._dbx.files_upload_session_start(chunk)
        offset = len(chunk)

        # Append chunks
        while True:
            chunk = stream.read(CHUNK_SIZE)
            if not chunk:
                break
            cursor = dropbox.files.UploadSessionCursor(
                session_id=session.session_id, offset=offset
            )
            self._dbx.files_upload_session_append_v2(chunk, cursor)
            offset += len(chunk)

        # Finish
        cursor = dropbox.files.UploadSessionCursor(
            session_id=session.session_id, offset=offset
        )
        commit = dropbox.files.CommitInfo(path=path, mode=mode)
        return self._dbx.files_upload_session_finish(b"", cursor, commit)

    def should_chunk(self, size: int) -> bool:
        """Return True if the file size requires chunked upload."""
        return size > CHUNK_THRESHOLD
