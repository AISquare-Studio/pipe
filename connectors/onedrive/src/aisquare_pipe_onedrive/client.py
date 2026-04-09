"""Shared Microsoft Graph API wrapper for OneDrive operations.

Uses azure-identity for authentication and requests for HTTP calls.
"""

from __future__ import annotations

import io
import logging
import time
from collections.abc import Iterator
from typing import IO, Any

import requests

from aisquare.pipe.errors import ConfigValidationError, PipelineError

from aisquare_pipe_onedrive.constants import (
    CHUNK_SIZE,
    GRAPH_BASE_URL,
    INITIAL_BACKOFF,
    MAX_RETRIES,
    SIMPLE_UPLOAD_LIMIT,
)

logger = logging.getLogger("aisquare.pipe.onedrive")


class OneDriveClient:
    """Wraps Microsoft Graph API for OneDrive file operations."""

    def __init__(self, config: dict[str, Any]) -> None:
        if "access_token" in config:
            self._token = config["access_token"]
        elif all(k in config for k in ("client_id", "client_secret", "tenant_id")):
            self._token = self._acquire_token_client_credentials(config)
        else:
            raise ConfigValidationError(
                "OneDrive config requires either 'access_token' or "
                "all of 'client_id', 'client_secret', 'tenant_id'"
            )

    @staticmethod
    def _acquire_token_client_credentials(config: dict[str, Any]) -> str:
        """Acquire token using OAuth2 client credentials flow."""
        from azure.identity import ClientSecretCredential

        try:
            credential = ClientSecretCredential(
                tenant_id=config["tenant_id"],
                client_id=config["client_id"],
                client_secret=config["client_secret"],
            )
            token = credential.get_token("https://graph.microsoft.com/.default")
            return token.token
        except Exception as e:
            raise ConfigValidationError(f"OneDrive auth failed: {e}") from e

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict | None = None,
        data: bytes | None = None,
        stream: bool = False,
    ) -> requests.Response:
        """Make an HTTP request with retry on 429/503/504."""
        merged_headers = self._headers()
        if headers:
            merged_headers.update(headers)

        for attempt in range(MAX_RETRIES):
            resp = requests.request(
                method,
                url,
                headers=merged_headers,
                json=json,
                data=data,
                stream=stream,
                timeout=300,
            )

            if resp.status_code in (429, 503, 504):
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else INITIAL_BACKOFF * (2**attempt)
                logger.warning(
                    "OneDrive throttled (HTTP %d, attempt %d/%d), retrying in %.1fs",
                    resp.status_code,
                    attempt + 1,
                    MAX_RETRIES,
                    wait,
                )
                time.sleep(wait)
                continue

            if resp.status_code == 401:
                raise ConfigValidationError(
                    f"OneDrive auth failed (401): {resp.text}"
                )

            if resp.status_code >= 400:
                raise PipelineError(
                    f"OneDrive API error (HTTP {resp.status_code}): {resp.text}"
                )

            return resp

        raise PipelineError("OneDrive rate limit exceeded after max retries")

    def validate(self) -> bool:
        """Test credentials by fetching the current user's drive."""
        self._request("GET", f"{GRAPH_BASE_URL}/me/drive")
        return True

    def list_folder(
        self, path: str = "", recursive: bool = False
    ) -> Iterator[dict[str, Any]]:
        """List folder contents with automatic pagination.

        Yields item dicts from the Graph API (files and folders).
        """
        if path and path != "/":
            url = f"{GRAPH_BASE_URL}/me/drive/root:/{path.strip('/')}:/children"
        else:
            url = f"{GRAPH_BASE_URL}/me/drive/root/children"

        try:
            while url:
                resp = self._request("GET", url)
                data = resp.json()
                yield from data.get("value", [])

                # Handle pagination
                url = data.get("@odata.nextLink")

            # If recursive, descend into subfolders
            if recursive:
                # Re-list to find folders (we already yielded everything)
                if path and path != "/":
                    folder_url = f"{GRAPH_BASE_URL}/me/drive/root:/{path.strip('/')}:/children"
                else:
                    folder_url = f"{GRAPH_BASE_URL}/me/drive/root/children"

                resp = self._request("GET", folder_url)
                items = resp.json().get("value", [])
                for item in items:
                    if "folder" in item:
                        subfolder_path = item.get("parentReference", {}).get("path", "")
                        item_path = f"{path.strip('/')}/{item['name']}".strip("/")
                        yield from self.list_folder(item_path, recursive=True)
        except ConfigValidationError:
            raise
        except PipelineError:
            raise

    def download(self, item_id: str) -> tuple[dict[str, Any], bytes]:
        """Download a file by item ID. Returns (metadata_dict, content_bytes)."""
        # Get metadata
        meta_resp = self._request("GET", f"{GRAPH_BASE_URL}/me/drive/items/{item_id}")
        metadata = meta_resp.json()

        # Download content
        content_resp = self._request(
            "GET",
            f"{GRAPH_BASE_URL}/me/drive/items/{item_id}/content",
        )
        return metadata, content_resp.content

    def download_stream(self, item_id: str) -> tuple[dict[str, Any], IO[bytes]]:
        """Download a file and return as stream. Caller must close."""
        meta_resp = self._request("GET", f"{GRAPH_BASE_URL}/me/drive/items/{item_id}")
        metadata = meta_resp.json()

        content_resp = self._request(
            "GET",
            f"{GRAPH_BASE_URL}/me/drive/items/{item_id}/content",
        )
        return metadata, io.BytesIO(content_resp.content)

    def upload(
        self, data: bytes, path: str, conflict: str = "rename"
    ) -> dict[str, Any]:
        """Upload a small file (< 4MB) via simple PUT.

        Args:
            conflict: "rename", "replace", or "fail"
        """
        url = (
            f"{GRAPH_BASE_URL}/me/drive/root:/{path.strip('/')}:/content"
            f"?@microsoft.graph.conflictBehavior={conflict}"
        )
        resp = self._request(
            "PUT",
            url,
            headers={"Content-Type": "application/octet-stream"},
            data=data,
        )
        return resp.json()

    def upload_chunked(
        self,
        stream: IO[bytes],
        path: str,
        size: int,
        conflict: str = "rename",
    ) -> dict[str, Any]:
        """Upload a large file using a resumable upload session.

        Args:
            stream: Readable bytes stream
            path: Target path in OneDrive
            size: Total file size in bytes
            conflict: Conflict behavior
        """
        # Create upload session
        session_url = (
            f"{GRAPH_BASE_URL}/me/drive/root:/{path.strip('/')}:/createUploadSession"
        )
        session_body = {
            "item": {
                "@microsoft.graph.conflictBehavior": conflict,
            }
        }
        session_resp = self._request("POST", session_url, json=session_body)
        upload_url = session_resp.json()["uploadUrl"]

        # Upload chunks
        offset = 0
        while offset < size:
            chunk = stream.read(CHUNK_SIZE)
            if not chunk:
                break

            end = offset + len(chunk) - 1
            chunk_headers = {
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {offset}-{end}/{size}",
            }

            resp = requests.put(
                upload_url,
                headers=chunk_headers,
                data=chunk,
                timeout=300,
            )

            if resp.status_code in (429, 503, 504):
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else INITIAL_BACKOFF
                logger.warning("Upload throttled, retrying in %.1fs", wait)
                time.sleep(wait)
                # Re-read from current position
                stream.seek(offset)
                continue

            if resp.status_code >= 400 and resp.status_code not in (200, 201, 202):
                raise PipelineError(
                    f"OneDrive upload chunk failed (HTTP {resp.status_code}): {resp.text}"
                )

            offset += len(chunk)

            # Final chunk returns 200/201 with the item metadata
            if resp.status_code in (200, 201):
                return resp.json()

        raise PipelineError("OneDrive upload session completed without final response")

    def should_chunk(self, size: int) -> bool:
        """Return True if the file requires a resumable upload session."""
        return size > SIMPLE_UPLOAD_LIMIT
