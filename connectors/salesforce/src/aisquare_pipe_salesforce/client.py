"""Thin Salesforce REST client + OAuth helpers.

Domain-pure: config in, API responses out. Token refresh/exchange RETURN the
new token payloads — persisting them is the host's job. ``session`` is
injectable so tests never touch the network.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

import requests

API_VERSION = "v60.0"
DEFAULT_AUTH_BASE_URL = "https://login.salesforce.com"
DEFAULT_TIMEOUT = 30


class SalesforceError(RuntimeError):
    def __init__(self, status_code: int, body: str = ""):
        self.status_code = status_code
        self.body = body[:512]
        super().__init__(f"Salesforce returned {status_code}: {self.body}")


class SalesforceAuthError(SalesforceError):
    """401/403 — token expired/revoked or scope missing."""


class SalesforceRateLimited(SalesforceError):
    """429 — caller should retry with backoff."""


def _raise_for_status(response: requests.Response) -> None:
    if response.status_code in (401, 403):
        raise SalesforceAuthError(response.status_code, response.text)
    if response.status_code == 429:
        raise SalesforceRateLimited(response.status_code, response.text)
    if not 200 <= response.status_code < 300:
        raise SalesforceError(response.status_code, response.text)


# ---------------------------------------------------------------------------
# OAuth (module-level pure helpers — host persists the returned tokens)
# ---------------------------------------------------------------------------


def authorize_url(config: dict[str, Any], redirect_uri: str, state: str) -> str:
    base = config.get("auth_base_url") or DEFAULT_AUTH_BASE_URL
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": config["client_id"],
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"{base}/services/oauth2/authorize?{query}"


def exchange_code(
    config: dict[str, Any],
    code: str,
    redirect_uri: str,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Authorization-code exchange. Returns the raw token payload
    (``access_token`` / ``refresh_token`` / ``instance_url`` …)."""
    base = config.get("auth_base_url") or DEFAULT_AUTH_BASE_URL
    response = (session or requests.Session()).post(
        f"{base}/services/oauth2/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
            "redirect_uri": redirect_uri,
        },
        timeout=DEFAULT_TIMEOUT,
    )
    _raise_for_status(response)
    return response.json()


def refresh_access_token(
    config: dict[str, Any], session: requests.Session | None = None
) -> dict[str, Any]:
    """Refresh-token grant. Returns the raw token payload."""
    base = config.get("auth_base_url") or DEFAULT_AUTH_BASE_URL
    response = (session or requests.Session()).post(
        f"{base}/services/oauth2/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": config["refresh_token"],
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
        },
        timeout=DEFAULT_TIMEOUT,
    )
    _raise_for_status(response)
    return response.json()


# ---------------------------------------------------------------------------
# REST client
# ---------------------------------------------------------------------------


class SalesforceClient:
    """Config keys: ``access_token`` + ``instance_url`` (required for REST);
    ``client_id`` / ``client_secret`` / ``refresh_token`` (OAuth helpers)."""

    def __init__(self, config: dict[str, Any], session: requests.Session | None = None):
        self._config = config
        self._session = session or requests.Session()

    def validate(self) -> bool:
        return bool(self._config.get("access_token") and self._config.get("instance_url"))

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._config['access_token']}"}

    def _base(self) -> str:
        return f"{self._config['instance_url']}/services/data/{API_VERSION}"

    def get_sobject(self, sobject: str, record_id: str) -> dict[str, Any]:
        response = self._session.get(
            f"{self._base()}/sobjects/{sobject}/{record_id}",
            headers=self._headers(),
            timeout=DEFAULT_TIMEOUT,
        )
        _raise_for_status(response)
        return response.json()

    def update_sobject(self, sobject: str, record_id: str, fields: dict[str, Any]) -> None:
        response = self._session.patch(
            f"{self._base()}/sobjects/{sobject}/{record_id}",
            headers={**self._headers(), "Content-Type": "application/json"},
            json=fields,
            timeout=DEFAULT_TIMEOUT,
        )
        _raise_for_status(response)

    def download_content_version(self, content_version_id: str) -> bytes:
        """ContentVersion.VersionData — the uploaded file's bytes."""
        response = self._session.get(
            f"{self._base()}/sobjects/ContentVersion/{content_version_id}/VersionData",
            headers=self._headers(),
            timeout=DEFAULT_TIMEOUT * 2,
        )
        _raise_for_status(response)
        return response.content
