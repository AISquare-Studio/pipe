"""Thin DocuSign eSignature REST client + OAuth helpers.

Domain-pure: config in, API responses out. Token exchange/refresh RETURN the
new token payloads — persisting them is the host's job. ``session`` is
injectable so tests never touch the network.
"""

from __future__ import annotations

import base64
import urllib.parse
from typing import Any

import requests

DEFAULT_AUTH_BASE_URL = "https://account-d.docusign.com"
DEFAULT_TIMEOUT = 30


class DocuSignError(RuntimeError):
    def __init__(self, status_code: int, body: str = ""):
        self.status_code = status_code
        self.body = body[:512]
        super().__init__(f"DocuSign returned {status_code}: {self.body}")


class DocuSignAuthError(DocuSignError):
    """401/403 — token expired/revoked or consent missing."""


class DocuSignRateLimited(DocuSignError):
    """429 — caller should retry with backoff."""


def _raise_for_status(response: requests.Response) -> None:
    if response.status_code in (401, 403):
        raise DocuSignAuthError(response.status_code, response.text)
    if response.status_code == 429:
        raise DocuSignRateLimited(response.status_code, response.text)
    if not 200 <= response.status_code < 300:
        raise DocuSignError(response.status_code, response.text)


def _basic_auth(config: dict[str, Any]) -> str:
    raw = f"{config['integration_key']}:{config['client_secret']}".encode()
    return base64.b64encode(raw).decode()


# ---------------------------------------------------------------------------
# OAuth (module-level pure helpers — host persists the returned tokens)
# ---------------------------------------------------------------------------


def authorize_url(config: dict[str, Any], redirect_uri: str, state: str) -> str:
    base = config.get("auth_base_url") or DEFAULT_AUTH_BASE_URL
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "scope": "signature",
            "client_id": config["integration_key"],
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"{base}/oauth/auth?{query}"


def exchange_code(
    config: dict[str, Any], code: str, session: requests.Session | None = None
) -> dict[str, Any]:
    base = config.get("auth_base_url") or DEFAULT_AUTH_BASE_URL
    response = (session or requests.Session()).post(
        f"{base}/oauth/token",
        headers={"Authorization": f"Basic {_basic_auth(config)}"},
        data={"grant_type": "authorization_code", "code": code},
        timeout=DEFAULT_TIMEOUT,
    )
    _raise_for_status(response)
    return response.json()


def refresh_access_token(
    config: dict[str, Any], session: requests.Session | None = None
) -> dict[str, Any]:
    base = config.get("auth_base_url") or DEFAULT_AUTH_BASE_URL
    response = (session or requests.Session()).post(
        f"{base}/oauth/token",
        headers={"Authorization": f"Basic {_basic_auth(config)}"},
        data={"grant_type": "refresh_token", "refresh_token": config["refresh_token"]},
        timeout=DEFAULT_TIMEOUT,
    )
    _raise_for_status(response)
    return response.json()


def user_info(
    config: dict[str, Any], session: requests.Session | None = None
) -> dict[str, Any]:
    """Resolve ``accounts[].account_id`` + ``base_uri`` after the exchange."""
    base = config.get("auth_base_url") or DEFAULT_AUTH_BASE_URL
    response = (session or requests.Session()).get(
        f"{base}/oauth/userinfo",
        headers={"Authorization": f"Bearer {config['access_token']}"},
        timeout=DEFAULT_TIMEOUT,
    )
    _raise_for_status(response)
    return response.json()


# ---------------------------------------------------------------------------
# REST client
# ---------------------------------------------------------------------------


class DocuSignClient:
    """Config keys: ``access_token`` + ``account_id`` + ``base_uri`` (REST);
    ``integration_key`` / ``client_secret`` / ``refresh_token`` (OAuth)."""

    def __init__(self, config: dict[str, Any], session: requests.Session | None = None):
        self._config = config
        self._session = session or requests.Session()

    def validate(self) -> bool:
        return bool(
            self._config.get("access_token")
            and self._config.get("account_id")
            and self._config.get("base_uri")
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._config['access_token']}"}

    def _base(self) -> str:
        return (
            f"{self._config['base_uri']}/restapi/v2.1/accounts/{self._config['account_id']}"
        )

    def create_envelope(self, payload: dict[str, Any]) -> str:
        response = self._session.post(
            f"{self._base()}/envelopes",
            headers={**self._headers(), "Content-Type": "application/json"},
            json=payload,
            timeout=DEFAULT_TIMEOUT * 2,
        )
        _raise_for_status(response)
        return response.json().get("envelopeId", "")

    def envelope_status(self, envelope_id: str) -> dict[str, Any]:
        response = self._session.get(
            f"{self._base()}/envelopes/{envelope_id}",
            headers=self._headers(),
            timeout=DEFAULT_TIMEOUT,
        )
        _raise_for_status(response)
        return response.json()

    def list_envelopes(self, from_date_iso: str) -> list[dict[str, Any]]:
        response = self._session.get(
            f"{self._base()}/envelopes",
            headers=self._headers(),
            params={"from_date": from_date_iso},
            timeout=DEFAULT_TIMEOUT,
        )
        _raise_for_status(response)
        return response.json().get("envelopes", [])

    def download_combined_document(self, envelope_id: str) -> bytes:
        response = self._session.get(
            f"{self._base()}/envelopes/{envelope_id}/documents/combined",
            headers=self._headers(),
            timeout=DEFAULT_TIMEOUT * 2,
        )
        _raise_for_status(response)
        return response.content
