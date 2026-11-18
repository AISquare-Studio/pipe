"""Databricks credentials: the tenant's blob, checked before any request.

The blob is one JSON object (see the README for the Secrets Manager name)::

    {"workspace_url": "https://acme.cloud.databricks.com",
     "auth": "pat", "token": "<personal access token>",
     "warehouse_id": "<SQL warehouse id, for DATABRICKS_RUN_SELECT>"}

    {"workspace_url": "https://adb-1234567890123456.7.azuredatabricks.net",
     "auth": "oauth_m2m", "client_id": "<service principal application id>",
     "client_secret": "<its OAuth secret>", "warehouse_id": "..."}

* ``workspace_url`` must be an https Databricks workspace host:
  ``*.cloud.databricks.com`` (AWS), ``*.azuredatabricks.net`` (Azure) or
  ``*.gcp.databricks.com`` (GCP), with no path, port or userinfo. It is never
  taken from a caller.
* ``auth`` ``pat`` needs ``token``; ``oauth_m2m`` needs ``client_id`` and
  ``client_secret`` (a service principal's OAuth secret: the token comes from
  the workspace's ``/oidc/v1/token``, scope ``all-apis``, and is cached in
  memory until five minutes before it expires).
* ``warehouse_id`` (optional) is the SQL warehouse SQL runs on; without it
  ``DATABRICKS_RUN_SELECT`` is "not configured".

Anything missing or malformed raises :class:`DatabricksNotConfigured` with a
reason that never contains a secret, and no request is made. Ambient
credentials (``DATABRICKS_*`` environment variables, ``~/.databrickscfg``)
are never read.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import requests

#: The workspace hosts Databricks serves: AWS, Azure and GCP.
WORKSPACE_HOST = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
    r"\.(?:cloud\.databricks\.com|azuredatabricks\.net|gcp\.databricks\.com)$"
)
AUTH_KINDS = ("pat", "oauth_m2m")
_WAREHOUSE_ID = re.compile(r"^[A-Za-z0-9]{1,64}$")
_REFRESH_SKEW_SECONDS = 300.0

#: The blob's keys. They are server-controlled: a caller may set none of them.
SERVER_KEYS = frozenset(
    {"workspace_url", "auth", "token", "client_id", "client_secret", "warehouse_id"}
)


class DatabricksNotConfigured(ValueError):
    """The Databricks credential blob is missing or unusable. The message
    names what is wrong and never includes a secret."""


class DatabricksTokenError(Exception):
    """The workspace refused the service principal's OAuth secret, or its
    token endpoint could not be reached (``unreachable``; ``timed_out`` when
    it did not answer in time)."""

    def __init__(
        self, message: str, *, unreachable: bool = False, timed_out: bool = False
    ) -> None:
        super().__init__(message)
        self.unreachable = unreachable
        self.timed_out = timed_out


@dataclass(frozen=True)
class DatabricksCredentials:
    host: str
    auth: str
    token: str | None = field(default=None, repr=False)
    client_id: str | None = None
    client_secret: str | None = field(default=None, repr=False)
    warehouse_id: str | None = None

    @property
    def base_url(self) -> str:
        return f"https://{self.host}"

    @property
    def secrets(self) -> tuple[str, ...]:
        """Every secret value, for redaction."""
        return tuple(s for s in (self.token, self.client_secret) if s)

    @property
    def cache_key(self) -> str:
        material = f"{self.host}\0{self.client_id}\0{self.client_secret}"
        return hashlib.sha256(material.encode()).hexdigest()


def _secret(config: Mapping[str, Any], key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DatabricksNotConfigured(f"{key} is missing")
    value = value.strip()
    if any(ch.isspace() for ch in value) or not value.isprintable():
        raise DatabricksNotConfigured(f"{key} holds whitespace or control characters")
    return value


def _host(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DatabricksNotConfigured(
            "workspace_url is missing (e.g. https://acme.cloud.databricks.com)"
        )
    refused = DatabricksNotConfigured(
        "workspace_url must be https://<workspace> on cloud.databricks.com, "
        "azuredatabricks.net or gcp.databricks.com (no port, path or userinfo)"
    )
    try:
        parts = urlsplit(value.strip())
        host = (parts.hostname or "").lower()
        port = parts.port  # raises ValueError on a port that is not a number
    except ValueError:
        raise refused from None
    if (
        parts.scheme != "https"
        or port is not None
        or parts.username is not None
        or parts.password is not None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or not WORKSPACE_HOST.match(host)
    ):
        raise refused
    return host


def load_credentials(config: Any) -> DatabricksCredentials:
    """The credentials in ``config`` (the tenant's blob), or
    :class:`DatabricksNotConfigured`. Pure: no network, no environment."""
    if not isinstance(config, Mapping):
        raise DatabricksNotConfigured("no Databricks credentials are configured")
    host = _host(config.get("workspace_url"))
    auth = config.get("auth", "pat")
    if auth not in AUTH_KINDS:
        raise DatabricksNotConfigured("auth must be 'pat' or 'oauth_m2m'")
    warehouse = config.get("warehouse_id")
    if warehouse in (None, ""):
        warehouse = None
    elif not isinstance(warehouse, str) or not _WAREHOUSE_ID.match(warehouse):
        raise DatabricksNotConfigured("warehouse_id must be a SQL warehouse id")
    if auth == "pat":
        return DatabricksCredentials(
            host=host, auth=auth, token=_secret(config, "token"), warehouse_id=warehouse
        )
    return DatabricksCredentials(
        host=host,
        auth=auth,
        client_id=_secret(config, "client_id"),
        client_secret=_secret(config, "client_secret"),
        warehouse_id=warehouse,
    )


_tokens: dict[str, tuple[str, float]] = {}
_tokens_lock = threading.Lock()


def clear_token_cache() -> None:
    """Forget every minted OAuth token (tests)."""
    with _tokens_lock:
        _tokens.clear()


def bearer_token(creds: DatabricksCredentials, session: Any, *, timeout: float) -> str:
    """The bearer for every call: the PAT, or a cached / freshly minted OAuth
    M2M token (client credentials at ``<workspace>/oidc/v1/token``)."""
    if creds.auth == "pat":
        assert creds.token is not None
        return creds.token
    now = time.time()
    with _tokens_lock:
        cached = _tokens.get(creds.cache_key)
        if cached is not None and cached[1] - _REFRESH_SKEW_SECONDS > now:
            return cached[0]
    try:
        resp = session.request(
            "POST",
            f"{creds.base_url}/oidc/v1/token",
            data={"grant_type": "client_credentials", "scope": "all-apis"},
            auth=(creds.client_id or "", creds.client_secret or ""),
            headers={"Accept": "application/json"},
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.RequestException as e:
        raise DatabricksTokenError(
            f"could not reach the workspace's token endpoint ({type(e).__name__})",
            unreachable=True,
            timed_out=isinstance(e, requests.Timeout),
        ) from None
    if resp.status_code != 200:
        raise DatabricksTokenError(
            f"the workspace did not issue an OAuth token for client {creds.client_id} "
            f"({resp.status_code})"
        )
    try:
        body = resp.json()
    except ValueError:
        body = None
    token = body.get("access_token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise DatabricksTokenError("the workspace's token response had no access_token")
    expires_in = body.get("expires_in") if isinstance(body, dict) else None
    lifetime = float(expires_in) if isinstance(expires_in, int | float) else 3600.0
    with _tokens_lock:
        _tokens[creds.cache_key] = (token, now + lifetime)
    return token


__all__ = [
    "AUTH_KINDS",
    "SERVER_KEYS",
    "DatabricksCredentials",
    "DatabricksNotConfigured",
    "DatabricksTokenError",
    "bearer_token",
    "clear_token_cache",
    "load_credentials",
]
