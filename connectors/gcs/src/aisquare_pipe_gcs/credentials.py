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
  refused. It must end with ``/`` (a folder such as ``policies/``), so
  ``policies/`` never admits ``policies-hr/``. The prefix is enforced HERE,
  by the connector: Google lets the key read whatever its IAM grant covers
  (see the README).

Application Default Credentials are never consulted: no
``google.auth.default()``, no ``GOOGLE_APPLICATION_CREDENTIALS``, no metadata
server. Anything missing or malformed raises :class:`GcsNotConfigured` with a
reason that never contains key material, and no request is made.

Access tokens are minted with the key (RFC 7523 JWT bearer, scope
``devstorage.read_only``): google-auth signs the assertion, and ONE POST to
Google's token endpoint, through the caller's session and inside its timeout
(no hidden retries or backoff sleeps), exchanges it. A failure says why
(:class:`GcsTokenError` ``kind``): Google refused the key, the endpoint timed
out, was unreachable, or failed (5xx). Tokens are kept in memory per key
until five minutes before they expire, so a host that builds a fresh toolkit
per call doesn't mint one per call; a token Cloud Storage rejects is dropped
(:func:`forget_token`).
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import requests
from google.auth import crypt, jwt

from aisquare_pipe_gcs._http import retry_after_seconds, timed_out

#: Read-only access to object data and metadata.
READ_ONLY_SCOPE = "https://www.googleapis.com/auth/devstorage.read_only"
#: The token endpoints a key may name: Google's only.
TOKEN_URIS = frozenset(
    {
        "https://oauth2.googleapis.com/token",
        "https://accounts.google.com/o/oauth2/token",
    }
)
#: The audience of every assertion (google-auth uses this one whatever the
#: key's token_uri).
_AUDIENCE = "https://oauth2.googleapis.com/token"
_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"
_ASSERTION_LIFETIME = 3600
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
    if (
        not isinstance(value, str)
        or not value.isprintable()
        or value.startswith("/")
        or len(value.encode()) > 1024
    ):
        raise GcsNotConfigured(
            "prefix must be an object-name prefix such as 'policies/'"
        )
    if not value.endswith("/"):
        # A bare startswith would let 'policies' admit 'policies-hr/...'.
        raise GcsNotConfigured(
            "prefix must end with '/' (a folder such as 'policies/'), so it can't "
            "admit a sibling such as 'policies-hr/'"
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


#: Why a token wasn't issued (:attr:`GcsTokenError.kind`).
TOKEN_REFUSED = "refused"  # Google rejected the key: 400/401/403 such as invalid_grant
TOKEN_TIMED_OUT = "timed_out"  # the token endpoint didn't answer in time
TOKEN_UNREACHABLE = "unreachable"  # no answer at all: DNS, connect, TLS, reset
TOKEN_RATE_LIMITED = "rate_limited"  # 429
TOKEN_UPSTREAM = "upstream"  # 5xx, a redirect, or an answer without a token
#: invalid_grant for the assertion's iat/exp: THIS host's clock is off, the
#: key is fine (Google: "Invalid JWT: Token must be a short-lived token (60
#: minutes) and in a reasonable timeframe. Check your iat and exp values...").
TOKEN_CLOCK_SKEW = "clock_skew"

#: OAuth error codes Google uses for its own transient failures (RFC 6749
#: 4.1.2.1), whatever the status: not the key's fault.
_TRANSIENT_ERRORS = frozenset(
    {"internal_failure", "server_error", "temporarily_unavailable"}
)
_CLOCK_SKEW = re.compile(r"\biat\b.*\bexp\b|reasonable timeframe", re.IGNORECASE)


class GcsTokenError(Exception):
    """Google's token endpoint did not issue a token. ``kind`` is one of the
    ``TOKEN_*`` values: only :data:`TOKEN_REFUSED` means the key itself is
    bad. ``status`` is the endpoint's HTTP status (None when nothing came
    back). The message never carries key material, the assertion or a
    token."""

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.status = status
        self.retry_after = retry_after


_tokens: dict[str, tuple[str, float]] = {}
_tokens_lock = threading.Lock()


def clear_token_cache() -> None:
    """Forget every minted token (tests)."""
    with _tokens_lock:
        _tokens.clear()


def forget_token(creds: GcsCredentials, token: str | None) -> None:
    """Drop ``token`` from the cache if it is still the one kept for this
    key: Cloud Storage rejected it, so no later call may reuse it. A newer
    token another call minted meanwhile stays."""
    if not token:
        return
    with _tokens_lock:
        cached = _tokens.get(creds.cache_key)
        if cached is not None and cached[0] == token:
            del _tokens[creds.cache_key]


def access_token(creds: GcsCredentials, session: Any, *, timeout: float) -> str:
    """A read-only access token for the key in ``creds`` (see
    :func:`obtain_token`)."""
    return obtain_token(creds, session, timeout=timeout)[0]


def obtain_token(
    creds: GcsCredentials, session: Any, *, timeout: float
) -> tuple[str, bool]:
    """``(token, from_cache)``: a cached token while it has more than five
    minutes left, else a new one minted through ``session`` (the transport
    every GCS call uses) with one POST bounded by ``timeout``.

    Raises :class:`GcsNotConfigured` when the key can't sign (wrong key type,
    corrupt PEM: the exception type only), and :class:`GcsTokenError` when
    Google issues no token."""
    now = time.time()
    with _tokens_lock:
        cached = _tokens.get(creds.cache_key)
        if cached is not None and cached[1] - _REFRESH_SKEW_SECONDS > now:
            return cached[0], True
    assertion = _assertion(creds, now)
    token, expiry = _exchange(creds, assertion, session, timeout=timeout, now=now)
    with _tokens_lock:
        _tokens[creds.cache_key] = (token, expiry)
    return token, False


def _assertion(creds: GcsCredentials, now: float) -> str:
    """The signed JWT (RFC 7523) google-auth's service account credentials
    would send: issuer the key's account, the read-only scope."""
    issued = int(now)
    payload = {
        "iat": issued,
        "exp": issued + _ASSERTION_LIFETIME,
        "iss": creds.client_email,
        "aud": _AUDIENCE,
        "scope": READ_ONLY_SCOPE,
    }
    try:
        signer = crypt.RSASigner.from_service_account_info(dict(creds.info))
        signed = jwt.encode(signer, payload)
    except Exception as e:  # noqa: BLE001 — any key the signer can't use
        # An EC key, a corrupt or encrypted PEM...: the tenant's blob must be
        # fixed. Only the exception's type: its text may quote the key.
        raise GcsNotConfigured(
            "service_account_json's private key can't be loaded or used to sign "
            f"(an RSA service account key is needed; {type(e).__name__})"
        ) from None
    return signed.decode() if isinstance(signed, bytes) else str(signed)


def _exchange(
    creds: GcsCredentials,
    assertion: str,
    session: Any,
    *,
    timeout: float,
    now: float,
) -> tuple[str, float]:
    """One POST of the assertion to the key's (Google-only) token endpoint."""
    who = creds.client_email
    try:
        resp = session.request(
            "POST",
            str(creds.info["token_uri"]),
            data=urlencode(
                {"grant_type": _GRANT_TYPE, "assertion": assertion}
            ).encode(),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.RequestException as e:
        if timed_out(e):
            raise GcsTokenError(
                "Google's token endpoint did not answer in time",
                kind=TOKEN_TIMED_OUT,
            ) from None
        raise GcsTokenError(
            f"could not reach Google's token endpoint ({type(e).__name__})",
            kind=TOKEN_UNREACHABLE,
        ) from None
    # Not streamed: the body was read (or failed, above) inside request().
    try:
        status = int(resp.status_code)
        try:
            data = resp.json()
        except ValueError:
            data = None
        retry_after = retry_after_seconds((resp.headers or {}).get("Retry-After"))
    finally:
        resp.close()
    data = data if isinstance(data, dict) else {}

    if status == 200:
        token = data.get("access_token")
        if not isinstance(token, str) or not token:
            raise GcsTokenError(
                f"Google's token endpoint issued no token for {who}",
                kind=TOKEN_UPSTREAM,
                status=status,
            )
        lifetime = data.get("expires_in")
        if isinstance(lifetime, bool) or not isinstance(lifetime, (int, float)):
            lifetime = _ASSERTION_LIFETIME
        return token, now + max(0.0, float(lifetime))

    error = data.get("error") if isinstance(data.get("error"), str) else ""
    detail = data.get("error_description")
    reason = ": ".join(
        x for x in (error, detail if isinstance(detail, str) else "") if x
    )
    reason = " ".join(reason.replace(assertion, "[redacted]").split())[:200]
    said = f": {reason}" if reason else ""
    if (
        status == 400
        and error == "invalid_grant"
        and isinstance(detail, str)
        and _CLOCK_SKEW.search(detail)
    ):
        raise GcsTokenError(
            "Google refused the token request's issue and expiry times: this "
            f"host's clock is off (the key is fine; check NTP){said}",
            kind=TOKEN_CLOCK_SKEW,
            status=status,
        )
    if status in (400, 401, 403) and error not in _TRANSIENT_ERRORS:
        raise GcsTokenError(
            f"Google did not issue a token for {who}{said}",
            kind=TOKEN_REFUSED,
            status=status,
        )
    if status == 429:
        raise GcsTokenError(
            f"Google's token endpoint is rate limiting ({status}){said}",
            kind=TOKEN_RATE_LIMITED,
            status=status,
            retry_after=retry_after,
        )
    if 300 <= status < 400:
        raise GcsTokenError(
            f"Google's token endpoint answered a redirect ({status}); redirects are "
            "not followed",
            kind=TOKEN_UPSTREAM,
            status=status,
        )
    raise GcsTokenError(
        f"Google's token endpoint failed ({status}){said}",
        kind=TOKEN_UPSTREAM,
        status=status,
        retry_after=retry_after if status == 503 else None,
    )


__all__ = [
    "READ_ONLY_SCOPE",
    "SERVER_KEYS",
    "TOKEN_CLOCK_SKEW",
    "TOKEN_RATE_LIMITED",
    "TOKEN_REFUSED",
    "TOKEN_TIMED_OUT",
    "TOKEN_UNREACHABLE",
    "TOKEN_UPSTREAM",
    "GcsCredentials",
    "GcsNotConfigured",
    "GcsTokenError",
    "access_token",
    "clear_token_cache",
    "forget_token",
    "load_credentials",
    "obtain_token",
]
