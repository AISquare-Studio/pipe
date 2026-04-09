"""Local filesystem client wrapper for aisquare.pipe."""

from __future__ import annotations

import functools
import logging
import os
import stat
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any

from aisquare.pipe.errors import ConfigValidationError, PipelineError

from aisquare_pipe_local.constants import WRITE_CHUNK_SIZE

logger = logging.getLogger("aisquare.pipe.local")


def _map_os_errors(func):  # type: ignore[type-arg]
    """Decorator that translates OS errors to framework errors."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except FileNotFoundError as e:
            raise PipelineError(f"File not found: {e}") from e
        except PermissionError as e:
            raise PipelineError(f"Permission denied: {e}") from e
        except IsADirectoryError as e:
            raise PipelineError(f"Is a directory: {e}") from e
        except OSError as e:
            raise PipelineError(f"Filesystem error: {e}") from e

    return wrapper


class LocalClient:
    """Wraps pathlib/os operations with path-safety, error translation, and convenience methods."""

    def __init__(self, config: dict[str, Any]) -> None:
        base = config.get("base_path")
        if not base or not isinstance(base, str):
            raise ConfigValidationError(
                "Local config requires 'base_path' (a non-empty directory path)"
            )
        self._base_path = Path(base).resolve()

    def _resolve_safe(self, path: str) -> Path:
        """Resolve a relative path within base_path, preventing directory traversal."""
        resolved = (self._base_path / path).resolve()
        if not resolved.is_relative_to(self._base_path):
            raise PipelineError(
                f"Path '{path}' resolves outside base_path '{self._base_path}'"
            )
        return resolved

    def validate(self, *, writable: bool = False) -> bool:
        """Check that base_path exists, is a directory, and is accessible."""
        if not self._base_path.exists():
            raise ConfigValidationError(
                f"base_path does not exist: {self._base_path}"
            )
        if not self._base_path.is_dir():
            raise ConfigValidationError(
                f"base_path is not a directory: {self._base_path}"
            )
        if not os.access(self._base_path, os.R_OK):
            raise ConfigValidationError(
                f"base_path is not readable: {self._base_path}"
            )
        if writable and not os.access(self._base_path, os.W_OK):
            raise ConfigValidationError(
                f"base_path is not writable: {self._base_path}"
            )
        return True

    @_map_os_errors
    def list_files(
        self, path: str = "", *, recursive: bool = False
    ) -> Iterator[Path]:
        """Yield Path objects for files under base_path/path.

        Skips directories. If recursive, walks the entire subtree.
        """
        target = self._resolve_safe(path)
        if recursive:
            for p in sorted(target.rglob("*")):
                if p.is_file():
                    yield p
        else:
            for p in sorted(target.iterdir()):
                if p.is_file():
                    yield p

    @_map_os_errors
    def list_entries(self, path: str = "") -> Iterator[Path]:
        """Yield all entries (files and directories) at one level for list_resources."""
        target = self._resolve_safe(path)
        yield from sorted(target.iterdir())

    @_map_os_errors
    def read_file(self, path: str) -> bytes:
        """Read an entire file into memory. Path is relative to base_path."""
        target = self._resolve_safe(path)
        return target.read_bytes()

    @_map_os_errors
    def read_stream(self, path: str) -> IO[bytes]:
        """Open a file for reading and return the file handle.

        Caller is responsible for closing the stream.
        """
        target = self._resolve_safe(path)
        return open(target, "rb")  # noqa: SIM115

    @_map_os_errors
    def write_file(
        self,
        data: bytes,
        path: str,
        *,
        conflict: str = "fail",
        create_dirs: bool = True,
    ) -> dict[str, Any]:
        """Write bytes to base_path/path.

        Args:
            data: bytes to write
            path: relative path within base_path
            conflict: "fail" (error if exists), "overwrite", or "rename" (append suffix)
            create_dirs: create parent directories if they don't exist

        Returns:
            Metadata dict with path, size, modified.
        """
        target = self._resolve_safe(path)
        target = self._handle_conflict(target, conflict)

        if create_dirs:
            target.parent.mkdir(parents=True, exist_ok=True)

        target.write_bytes(data)
        return self._file_meta(target)

    @_map_os_errors
    def write_stream(
        self,
        stream: IO[bytes],
        path: str,
        *,
        conflict: str = "fail",
        create_dirs: bool = True,
    ) -> dict[str, Any]:
        """Write from a stream to base_path/path in chunks."""
        target = self._resolve_safe(path)
        target = self._handle_conflict(target, conflict)

        if create_dirs:
            target.parent.mkdir(parents=True, exist_ok=True)

        with open(target, "wb") as f:
            while True:
                chunk = stream.read(WRITE_CHUNK_SIZE)
                if not chunk:
                    break
                f.write(chunk)

        return self._file_meta(target)

    def file_stat(self, path: str) -> dict[str, Any]:
        """Return stat info for a file relative to base_path."""
        target = self._resolve_safe(path)
        return self._file_meta(target)

    def _handle_conflict(self, target: Path, conflict: str) -> Path:
        """Resolve file conflict according to the chosen strategy."""
        if not target.exists():
            return target

        if conflict == "overwrite":
            return target
        elif conflict == "rename":
            return self._unique_path(target)
        else:
            # "fail" or unknown
            raise PipelineError(f"File already exists: {target}")

    @staticmethod
    def _unique_path(path: Path) -> Path:
        """Find a unique filename by appending _1, _2, etc."""
        stem = path.stem
        suffix = path.suffix
        parent = path.parent
        counter = 1
        while True:
            candidate = parent / f"{stem}_{counter}{suffix}"
            if not candidate.exists():
                return candidate
            counter += 1

    @staticmethod
    def _file_meta(path: Path) -> dict[str, Any]:
        """Build a metadata dict from a file's stat info."""
        st = path.stat()
        return {
            "path": str(path),
            "size": st.st_size,
            "modified": datetime.fromtimestamp(
                st.st_mtime, tz=timezone.utc
            ).isoformat(),
            "created": datetime.fromtimestamp(
                st.st_ctime, tz=timezone.utc
            ).isoformat(),
            "permissions": stat.filemode(st.st_mode),
        }
