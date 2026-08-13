"""Drive v3 REST wrapper for the Google Drive connector.

Auth uses google-auth directly rather than google-api-python-client: the
discovery-based client is heavy and we need only a handful of endpoints.
AuthorizedSession refreshes tokens transparently on each request.

Credentials never reach argv and are never logged — only their kind is.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import requests
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials

from aisquare.pipe.errors import ConfigValidationError, PipelineError

from aisquare_pipe_gdrive.constants import (
    INITIAL_BACKOFF,
    MAX_RETRIES,
    REQUEST_TIMEOUT,
    SCOPE_FULL,
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
            "https://www.googleapis.com/drive/v3/about",
            params={"fields": "user"},
        )
        return True
