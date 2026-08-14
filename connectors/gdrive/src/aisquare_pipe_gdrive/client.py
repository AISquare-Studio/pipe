"""Drive v3 REST wrapper for the Google Drive connector.

Auth uses google-auth directly rather than google-api-python-client: the
discovery-based client is heavy and we need only a handful of endpoints.
AuthorizedSession refreshes tokens transparently on each request.

Credentials never reach argv and are never logged — only their kind is.
"""

from __future__ import annotations

import io
import json
import logging
import os
import time
import uuid
from collections.abc import Iterator
from typing import IO, Any

import requests
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials

from aisquare.pipe.errors import ConfigValidationError, PipelineError

from aisquare_pipe_gdrive.constants import (
    CHUNK_SIZE,
    DRIVE_API_BASE,
    DRIVE_UPLOAD_BASE,
    EXPORT_SIZE_LIMIT,
    FILE_FIELDS,
    FOLDER_MIME,
    INITIAL_BACKOFF,
    MAX_RETRIES,
    REQUEST_TIMEOUT,
    SCOPE_FULL,
    SIMPLE_UPLOAD_LIMIT,
)

logger = logging.getLogger("aisquare.pipe.gdrive")

_RETRY_STATUS = (429, 500, 502, 503, 504)
_ERROR_BODY_LIMIT = 800


class GDriveClient:
    """Wraps the Drive v3 API for file listing, download, export and upload."""

    def __init__(self, config: dict[str, Any]) -> None:
        config = config or {}
        scopes = config.get("scopes") or [SCOPE_FULL]
        credentials = self._resolve_credentials(config, scopes)
        self._session = AuthorizedSession(credentials)
        self._drive_id: str = config.get("drive_id") or ""

    @staticmethod
    def _resolve_credentials(config: dict[str, Any], scopes: list[str]):
        """Build credentials from config. Offline — no network, no token fetch.

        Precedence: service account, then refresh token, then bare access token.
        """
        sa = config.get("service_account_json")
        if sa:
            try:
                if isinstance(sa, dict):
                    creds = service_account.Credentials.from_service_account_info(
                        sa, scopes=scopes
                    )
                elif isinstance(sa, str) and os.path.exists(sa):
                    creds = service_account.Credentials.from_service_account_file(
                        sa, scopes=scopes
                    )
                elif isinstance(sa, str):
                    creds = service_account.Credentials.from_service_account_info(
                        json.loads(sa), scopes=scopes
                    )
                else:
                    raise ConfigValidationError(
                        "gdrive 'service_account_json' must be a dict, a JSON string, "
                        "or a path to a key file"
                    )
            except ConfigValidationError:
                raise
            except Exception as e:  # noqa: BLE001 — message must not leak the key
                raise ConfigValidationError(
                    f"gdrive service account credentials are invalid: {type(e).__name__}"
                ) from e
            subject = config.get("subject")
            if subject:
                creds = creds.with_subject(subject)
            logger.debug("gdrive: authenticating with a service account")
            return creds

        if all(config.get(k) for k in ("client_id", "client_secret", "refresh_token")):
            logger.debug("gdrive: authenticating with an OAuth refresh token")
            return Credentials(
                token=None,
                refresh_token=config["refresh_token"],
                token_uri="https://oauth2.googleapis.com/token",
                client_id=config["client_id"],
                client_secret=config["client_secret"],
                scopes=scopes,
            )

        if config.get("access_token"):
            logger.debug("gdrive: authenticating with a bare access token")
            return Credentials(token=config["access_token"], scopes=scopes)

        raise ConfigValidationError(
            "gdrive config requires 'service_account_json', or "
            "'client_id' + 'client_secret' + 'refresh_token', or 'access_token'"
        )

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: dict | None = None,
        data: bytes | None = None,
        stream: bool = False,
    ) -> requests.Response:
        """Issue a request, retrying on 429/5xx and mapping errors to PipeErrors."""
        for attempt in range(MAX_RETRIES):
            resp = self._session.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                data=data,
                stream=stream,
                timeout=REQUEST_TIMEOUT,
            )

            if resp.status_code in _RETRY_STATUS:
                retry_after = resp.headers.get("Retry-After")
                wait = (
                    float(retry_after)
                    if retry_after
                    else INITIAL_BACKOFF * (2**attempt)
                )
                logger.warning(
                    "gdrive throttled (HTTP %d, attempt %d/%d), retrying in %.1fs",
                    resp.status_code,
                    attempt + 1,
                    MAX_RETRIES,
                    wait,
                )
                time.sleep(wait)
                continue

            if resp.status_code in (401, 403) and self._is_auth_failure(resp):
                raise ConfigValidationError(
                    f"gdrive auth failed (HTTP {resp.status_code}): "
                    f"{self._body_tail(resp)}"
                )

            if resp.status_code >= 400:
                raise PipelineError(
                    f"gdrive API error (HTTP {resp.status_code}): {self._body_tail(resp)}"
                )

            return resp

        raise PipelineError(f"gdrive rate limit exceeded after {MAX_RETRIES} retries")

    @staticmethod
    def _is_auth_failure(resp: requests.Response) -> bool:
        """A 401 is always auth; a 403 is auth only when it is not a quota or
        export-size refusal (those are surfaced by their own call sites)."""
        if resp.status_code == 401:
            return True
        return "insufficient" in (resp.text or "").lower()

    @staticmethod
    def _body_tail(resp: requests.Response) -> str:
        """Truncated response body — full bodies can carry tokens back in echoes."""
        return (resp.text or "")[:_ERROR_BODY_LIMIT]

    def validate(self) -> bool:
        """Confirm credentials work by fetching the About resource."""
        self._request(
            "GET",
            f"{DRIVE_API_BASE}/about",
            params={"fields": "user"},
        )
        return True

    def _shared_drive_params(self) -> dict[str, Any]:
        """Params that let a query see shared drives, when one is configured."""
        if not self._drive_id:
            return {}
        return {
            "driveId": self._drive_id,
            "corpora": "drive",
            "includeItemsFromAllDrives": True,
            "supportsAllDrives": True,
        }

    def list_files(
        self,
        folder_id: str = "root",
        recursive: bool = False,
        query: str = "",
    ) -> Iterator[dict[str, Any]]:
        """Yield Drive file resources under folder_id, following pagination.

        Folders are yielded like any other item; callers filter them out. When
        recursive is set, subfolders are descended into breadth-first after the
        current level is exhausted.
        """
        clauses = [f"'{folder_id}' in parents", "trashed = false"]
        if query:
            clauses.append(f"({query})")
        params: dict[str, Any] = {
            "q": " and ".join(clauses),
            "fields": f"nextPageToken, files({FILE_FIELDS})",
            "pageSize": 100,
            **self._shared_drive_params(),
        }

        subfolders: list[str] = []
        page_token: str | None = None
        while True:
            if page_token:
                params["pageToken"] = page_token
            resp = self._request("GET", f"{DRIVE_API_BASE}/files", params=params)
            payload = resp.json()
            for item in payload.get("files", []):
                if recursive and item.get("mimeType") == FOLDER_MIME:
                    subfolders.append(item["id"])
                yield item
            page_token = payload.get("nextPageToken")
            if not page_token:
                break

        for subfolder_id in subfolders:
            yield from self.list_files(subfolder_id, recursive=True, query=query)

    def download(self, file_id: str) -> bytes:
        """Download a binary file's content in full."""
        resp = self._request(
            "GET",
            f"{DRIVE_API_BASE}/files/{file_id}",
            params={"alt": "media", **self._shared_drive_params()},
        )
        return resp.content

    def download_stream(self, file_id: str) -> IO[bytes]:
        """Download a file as a stream. The caller must close it."""
        resp = self._request(
            "GET",
            f"{DRIVE_API_BASE}/files/{file_id}",
            params={"alt": "media", **self._shared_drive_params()},
            stream=True,
        )
        resp.raw.decode_content = True
        return resp.raw

    def export(self, file_id: str, target_mime: str) -> bytes:
        """Export a Google-native file to target_mime.

        Drive caps exported output at EXPORT_SIZE_LIMIT regardless of format, and
        signals the refusal as a 403 with reason exportSizeLimitExceeded. That is
        surfaced as a clear PipelineError rather than a bare HTTP error.
        """
        try:
            resp = self._request(
                "GET",
                f"{DRIVE_API_BASE}/files/{file_id}/export",
                params={"mimeType": target_mime},
            )
        except PipelineError as e:
            if "exportSizeLimitExceeded" in str(e):
                limit_mb = EXPORT_SIZE_LIMIT // (1024 * 1024)
                raise PipelineError(
                    f"gdrive cannot export file {file_id}: Drive caps exported "
                    f"content at {limit_mb} MB and this document exceeds it"
                ) from e
            raise
        return resp.content

    def should_chunk(self, size: int) -> bool:
        """Return True when a file needs a resumable upload session."""
        return size > SIMPLE_UPLOAD_LIMIT

    def find_child(self, name: str, parent_id: str) -> dict[str, Any] | None:
        """Return the first non-trashed child of parent_id named `name`."""
        escaped = name.replace("\\", "\\\\").replace("'", "\\'")
        resp = self._request(
            "GET",
            f"{DRIVE_API_BASE}/files",
            params={
                "q": (
                    f"name = '{escaped}' and '{parent_id}' in parents "
                    "and trashed = false"
                ),
                "fields": "files(id,name,mimeType)",
                "pageSize": 1,
                **self._shared_drive_params(),
            },
        )
        files = resp.json().get("files", [])
        return files[0] if files else None

    def create_folder(self, name: str, parent_id: str) -> str:
        """Create a folder under parent_id and return its ID."""
        resp = self._request(
            "POST",
            f"{DRIVE_API_BASE}/files",
            json_body={
                "name": name,
                "mimeType": FOLDER_MIME,
                "parents": [parent_id],
            },
            params={"fields": "id", **self._shared_drive_params()},
        )
        return resp.json()["id"]

    def ensure_folder_path(self, path: str, parent_id: str) -> str:
        """Resolve a slash-separated path under parent_id, creating as needed."""
        current = parent_id
        for segment in [s for s in path.split("/") if s]:
            existing = self.find_child(segment, current)
            if existing and existing.get("mimeType") == FOLDER_MIME:
                current = existing["id"]
            else:
                current = self.create_folder(segment, current)
        return current

    def upload(
        self, name: str, parent_id: str, mime_type: str, data: bytes
    ) -> dict[str, Any]:
        """Upload a small file via a multipart request (metadata + content)."""
        metadata = {"name": name, "parents": [parent_id]}
        boundary = f"aisquare-{uuid.uuid4().hex}"
        body = (
            f"--{boundary}\r\n"
            "Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(metadata)}\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode("utf-8") + data + f"\r\n--{boundary}--\r\n".encode("utf-8")

        resp = self._request(
            "POST",
            f"{DRIVE_UPLOAD_BASE}/files",
            headers={"Content-Type": f"multipart/related; boundary={boundary}"},
            params={
                "uploadType": "multipart",
                "fields": "id,name,size,webViewLink",
                **self._shared_drive_params(),
            },
            data=body,
        )
        return resp.json()

    def update_content(
        self, file_id: str, mime_type: str, data: bytes
    ) -> dict[str, Any]:
        """Replace an existing file's content, keeping its ID."""
        resp = self._request(
            "PATCH",
            f"{DRIVE_UPLOAD_BASE}/files/{file_id}",
            headers={"Content-Type": mime_type},
            params={
                "uploadType": "media",
                "fields": "id,name,size,webViewLink",
                **self._shared_drive_params(),
            },
            data=data,
        )
        return resp.json()

    def upload_resumable(
        self,
        name: str,
        parent_id: str,
        mime_type: str,
        stream: IO[bytes],
        size: int,
    ) -> dict[str, Any]:
        """Upload a large file through a resumable session.

        Chunks must be a multiple of 256 KiB; CHUNK_SIZE satisfies that. Drive
        answers each intermediate chunk with 308 and the final chunk with 200/201.
        """
        session = self._request(
            "POST",
            f"{DRIVE_UPLOAD_BASE}/files",
            headers={"X-Upload-Content-Type": mime_type},
            params={"uploadType": "resumable", **self._shared_drive_params()},
            json_body={"name": name, "parents": [parent_id]},
        )
        upload_url = session.headers.get("Location")
        if not upload_url:
            raise PipelineError("gdrive resumable upload session returned no Location")

        offset = 0
        while offset < size:
            chunk = stream.read(CHUNK_SIZE)
            if not chunk:
                break
            end = offset + len(chunk) - 1

            for attempt in range(MAX_RETRIES):
                resp = self._session.request(
                    "PUT",
                    upload_url,
                    headers={
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {offset}-{end}/{size}",
                    },
                    data=chunk,
                    timeout=REQUEST_TIMEOUT,
                )

                if resp.status_code in _RETRY_STATUS:
                    retry_after = resp.headers.get("Retry-After")
                    wait = (
                        float(retry_after)
                        if retry_after
                        else INITIAL_BACKOFF * (2**attempt)
                    )
                    logger.warning(
                        "gdrive chunk upload throttled (HTTP %d, attempt %d/%d), "
                        "retrying in %.1fs",
                        resp.status_code,
                        attempt + 1,
                        MAX_RETRIES,
                        wait,
                    )
                    time.sleep(wait)
                    continue

                if resp.status_code in (200, 201):
                    return resp.json()

                if resp.status_code != 308:
                    raise PipelineError(
                        f"gdrive upload chunk failed (HTTP {resp.status_code}): "
                        f"{self._body_tail(resp)}"
                    )

                offset += len(chunk)
                break
            else:
                raise PipelineError(
                    f"gdrive upload chunk at offset {offset} exhausted {MAX_RETRIES} "
                    "retries and did not complete"
                )

        raise PipelineError("gdrive upload session ended without a final response")
