"""Google Cloud Storage credentials: the tenant's blob, checked before any request.

The blob is one JSON object (see the README for the Secrets Manager name)::

    {"service_account_json": {<the service account's JSON key>} | "<that JSON as text>",
     "bucket": "acme-policies",
     "prefix": "policies/"}

* ``service_account_json`` is a service account key (``"type":
  "service_account"``) with ``roles/storage.objectViewer`` on the bucket. Its
  ``token_uri`` must be Google's (``https://oauth2.googleapis.com/token``) and
  its ``universe_domain`` (if any) ``googleapis.com``: the signed assertion is
  only ever sent to Google.
* ``bucket`` is the only bucket the connector reads. No tool argument or pull
  param can name another.
* ``prefix`` (optional) bounds every read: object names outside it are
  refused.

Application Default Credentials are never consulted: no
``google.auth.default()``, no ``GOOGLE_APPLICATION_CREDENTIALS``, no metadata
server. Anything missing or malformed raises :class:`GcsNotConfigured` with a
reason that never contains key material, and no request is made.

Access tokens are minted with the key (RFC 7523 JWT bearer, scope
``devstorage.read_only``) and kept in memory per key until five minutes
before they expire, so a host that builds a fresh toolkit per call doesn't
mint one per call.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any

import google.auth.exceptions
from google.auth.transport.requests import Request
from google.oauth2 import service_account

#: Read-only access to object data and metadata.
READ_ONLY_SCOPE = "https://www.googleapis.com/auth/devstorage.read_only"
#: The token endpoints a key may name: Google's only.
TOKEN_URIS = frozenset(
    {
        "https://oauth2.googleapis.com/token",
        "https://accounts.google.com/o/oauth2/token",
    }
)
_UNIVERSE = "googleapis.com"
#: GCS bucket names: 3-63 characters (222 with dots), lowercase letters,
#: digits, dashes, underscores and dots, starting and ending alphanumeric.
_BUCKET = re.compile(r"^(?=.{3,222}$)[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$")
#: A token is re-minted this long before it expires.
_REFRESH_SKEW_SECONDS = 300.0

#: The blob's keys. They are server-controlled: a caller may set none of them
#: (``prefix`` as a pull param is a narrower prefix INSIDE this one).
SERVER_KEYS = frozenset({"service_account_json", "bucket"})


class GcsNotConfigured(ValueError):
    """The GCS credential blob is missing or unusable. The message names what
    is wrong and never includes key material."""


@dataclass(frozen=True)
class GcsCredentials:
    bucket: str
    prefix: str
    client_email: str
    info: Mapping[str, Any] = field(repr=False)

    def contains(self, name: str) -> bool:
        """Whether object ``name`` is inside the configured prefix."""
        return name.startswith(self.prefix)

    @property
    def cache_key(self) -> str:
        """Which key minted a token (never the key itself)."""
        material = f"{self.client_email}\0{self.info.get('private_key_id', '')}\0"
        material += str(self.info.get("private_key", ""))
        return hashlib.sha256(material.encode()).hexdigest()


def _key_info(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise GcsNotConfigured("service_account_json is not valid JSON") from None
    if not isinstance(value, Mapping):
        raise GcsNotConfigured(
            "service_account_json is missing (a service account JSON key)"
        )
    info = dict(value)
    if info.get("type") != "service_account":
        raise GcsNotConfigured("service_account_json is not a service account key")
    for key in ("client_email", "private_key"):
        if not isinstance(info.get(key), str) or not info[key].strip():
            raise GcsNotConfigured(f"service_account_json has no {key}")
    if "BEGIN" not in info["private_key"] or "PRIVATE KEY" not in info["private_key"]:
        raise GcsNotConfigured(
            "service_account_json's private_key is not a PEM private key"
        )
    token_uri = info.setdefault("token_uri", "https://oauth2.googleapis.com/token")
    if token_uri not in TOKEN_URIS:
        raise GcsNotConfigured(
            "service_account_json's token_uri is not Google's token endpoint"
        )
    if info.get("universe_domain", _UNIVERSE) != _UNIVERSE:
        raise GcsNotConfigured("service_account_json is not for googleapis.com")
    return info


def _prefix(value: Any) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or not value.isprintable() or value.startswith("/"):
        raise GcsNotConfigured(
            "prefix must be an object-name prefix such as 'policies/'"
        )
    return value


def load_credentials(config: Any) -> GcsCredentials:
    """The credentials in ``config`` (the tenant's blob), or
    :class:`GcsNotConfigured`. Pure: no network, no environment."""
    if not isinstance(config, Mapping):
        raise GcsNotConfigured("no Google Cloud Storage credentials are configured")
    info = _key_info(config.get("service_account_json"))
    bucket = config.get("bucket")
    if not isinstance(bucket, str) or not _BUCKET.match(bucket):
        raise GcsNotConfigured("bucket is missing or is not a bucket name")
    return GcsCredentials(
        bucket=bucket,
        prefix=_prefix(config.get("prefix")),
        client_email=str(info["client_email"]).strip(),
        info=info,
    )


class GcsTokenError(Exception):
    """Google refused to mint a token for the key (revoked, disabled, wrong
    clock). The message never carries key material."""


_tokens: dict[str, tuple[str, float]] = {}
_tokens_lock = threading.Lock()


def clear_token_cache() -> None:
    """Forget every minted token (tests)."""
    with _tokens_lock:
        _tokens.clear()


def access_token(creds: GcsCredentials, session: Any, *, timeout: float) -> str:
    """A read-only access token for the key in ``creds``: a cached one while
    it has more than five minutes left, else a new one minted through
    ``session`` (the same transport every GCS call uses)."""
    now = time.time()
    with _tokens_lock:
        cached = _tokens.get(creds.cache_key)
        if cached is not None and cached[1] - _REFRESH_SKEW_SECONDS > now:
            return cached[0]
    try:
        signer = service_account.Credentials.from_service_account_info(
            dict(creds.info), scopes=[READ_ONLY_SCOPE]
        )
    except (ValueError, TypeError) as e:
        raise GcsNotConfigured(
            f"service_account_json's private key can't be loaded ({type(e).__name__})"
        ) from None
    request = Request(session=session)
    try:
        signer.refresh(_Timed(request, timeout))
    except google.auth.exceptions.RefreshError as e:
        reason = e.args[0] if e.args and isinstance(e.args[0], str) else "refused"
        raise GcsTokenError(
            f"Google did not issue a token for {creds.client_email}: {reason[:200]}"
        ) from None
    except google.auth.exceptions.TransportError as e:
        raise GcsTokenError(
            f"could not reach Google's token endpoint ({type(e).__name__})"
        ) from None
    token = str(signer.token or "")
    if not token:
        raise GcsTokenError(f"Google issued no token for {creds.client_email}")
    expiry = now + 3600
    if signer.expiry is not None:
        # google-auth keeps a naive UTC datetime.
        expires = signer.expiry
        expiry = (
            expires if expires.tzinfo else expires.replace(tzinfo=UTC)
        ).timestamp()
    with _tokens_lock:
        _tokens[creds.cache_key] = (token, expiry)
    return token


class _Timed:
    """google-auth's transport with a timeout on the token request."""

    def __init__(self, request: Request, timeout: float) -> None:
        self._request = request
        self._timeout = timeout

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: Any = None,
        headers: Any = None,
        **kw: Any,
    ) -> Any:
        kw.setdefault("timeout", self._timeout)
        return self._request(url, method=method, body=body, headers=headers, **kw)


__all__ = [
    "READ_ONLY_SCOPE",
    "SERVER_KEYS",
    "GcsCredentials",
    "GcsNotConfigured",
    "GcsTokenError",
    "access_token",
    "clear_token_cache",
    "load_credentials",
]
