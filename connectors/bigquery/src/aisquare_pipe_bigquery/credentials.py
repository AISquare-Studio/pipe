"""BigQuery credentials: the tenant's blob, checked before any request.

The blob is one JSON object (see the README for the secret's name)::

    {"service_account_json": {<the service account's JSON key>} | "<that JSON as text>",
     "project_id": "acme-analytics",
     "location": "US",
     "max_bytes_billed": 10737418240}

* ``service_account_json`` is a service account key (``"type":
  "service_account"``) holding ``roles/bigquery.jobUser`` on ``project_id``
  and ``roles/bigquery.dataViewer`` on the datasets it may read. Its
  ``token_uri`` must be Google's (``https://oauth2.googleapis.com/token``)
  and its ``universe_domain`` (if any) ``googleapis.com``: the signed
  assertion is only ever sent to Google.
* ``project_id`` is the project every query job runs in, and is billed to.
  No tool argument or pull param can change it. (A query or a table
  reference may still NAME another project's data: IAM decides whether the
  key can read it.)
* ``location`` (optional): where jobs run (``US``, ``EU``, ``us-central1``,
  ...). Without it, BigQuery picks the location of the data the query reads.
* ``max_bytes_billed`` (optional, bytes): the tenant's ceiling on the bytes
  one query may process. A caller may ask for less, never more. Default
  :data:`DEFAULT_MAX_BYTES_BILLED` (10 GiB).

Application Default Credentials are never consulted: no
``google.auth.default()``, no ``GOOGLE_APPLICATION_CREDENTIALS``, no metadata
server. Anything missing or malformed raises :class:`BigQueryNotConfigured`
with a reason that never contains key material, and no request is made.

Access tokens are minted with the key (RFC 7523 JWT bearer, scope
``bigquery.readonly``): google-auth signs the assertion, and ONE POST to
Google's token endpoint, through the caller's session and inside its timeout
(no hidden retries or backoff sleeps), exchanges it. A failure says why
(:class:`BigQueryTokenError` ``kind``): Google refused the key, the endpoint
timed out, was unreachable, or failed (5xx). Tokens are kept in memory per
key until five minutes before they expire, so a host that builds a fresh
toolkit per call doesn't mint one per call; a token BigQuery rejects is
dropped (:func:`forget_token`).
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

from aisquare_pipe_bigquery._http import retry_after_seconds, timed_out

#: Read-only BigQuery: datasets.list, tables.list/get, tabledata.list,
#: jobs.query and jobs.getQueryResults all accept it; jobs.insert (load,
#: extract and copy jobs) and jobs.cancel don't.
READ_ONLY_SCOPE = "https://www.googleapis.com/auth/bigquery.readonly"
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
#: A Google Cloud project id (6-30 characters: lowercase letters, digits and
#: dashes, starting with a letter), optionally domain-scoped
#: (``example.com:my-project``).
PROJECT_ID = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9.-]{0,61}[a-z0-9])?:)?[a-z][a-z0-9-]{4,28}[a-z0-9]$"
)
#: A BigQuery location: a multi-region (US, EU) or a region (us-central1).
_LOCATION = re.compile(r"^(?=.{2,64}$)[A-Za-z]+(?:-[a-z]+[0-9]*)*$")
#: A token is re-minted this long before it expires.
_REFRESH_SKEW_SECONDS = 300.0

GIB = 1024**3
#: The ceiling on the bytes one query may process when the blob sets none.
DEFAULT_MAX_BYTES_BILLED = 10 * GIB
#: The most a blob's ceiling may be (1 TiB), and the least: BigQuery bills
#: at least 10 MiB per table a query reads, so a lower cap refuses them all.
MAX_BYTES_BILLED_LIMIT = 1024 * GIB
MIN_BYTES_BILLED = 10 * 1024**2

#: The blob's keys. They are server-controlled: a caller may set none of them.
SERVER_KEYS = frozenset(
    {"service_account_json", "project_id", "location", "max_bytes_billed"}
)


class BigQueryNotConfigured(ValueError):
    """The BigQuery credential blob is missing or unusable. The message names
    what is wrong and never includes key material."""


@dataclass(frozen=True)
class BigQueryCredentials:
    project_id: str
    location: str | None
    max_bytes_billed: int
    client_email: str
    info: Mapping[str, Any] = field(repr=False)

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
            raise BigQueryNotConfigured(
                "service_account_json is not valid JSON"
            ) from None
    if not isinstance(value, Mapping):
        raise BigQueryNotConfigured(
            "service_account_json is missing (a service account JSON key)"
        )
    info = dict(value)
    if info.get("type") != "service_account":
        raise BigQueryNotConfigured("service_account_json is not a service account key")
    for key in ("client_email", "private_key"):
        if not isinstance(info.get(key), str) or not info[key].strip():
            raise BigQueryNotConfigured(f"service_account_json has no {key}")
    if "BEGIN" not in info["private_key"] or "PRIVATE KEY" not in info["private_key"]:
        raise BigQueryNotConfigured(
            "service_account_json's private_key is not a PEM private key"
        )
    token_uri = info.setdefault("token_uri", "https://oauth2.googleapis.com/token")
    if token_uri not in TOKEN_URIS:
        raise BigQueryNotConfigured(
            "service_account_json's token_uri is not Google's token endpoint"
        )
    if info.get("universe_domain", _UNIVERSE) != _UNIVERSE:
        raise BigQueryNotConfigured("service_account_json is not for googleapis.com")
    return info


def _location(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not _LOCATION.match(value):
        raise BigQueryNotConfigured(
            "location must be a BigQuery location such as 'US', 'EU' or 'us-central1'"
        )
    return value


def _ceiling(value: Any) -> int:
    if value is None:
        return DEFAULT_MAX_BYTES_BILLED
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not MIN_BYTES_BILLED <= value <= MAX_BYTES_BILLED_LIMIT
    ):
        raise BigQueryNotConfigured(
            f"max_bytes_billed must be a whole number of bytes from {MIN_BYTES_BILLED} "
            f"(10 MiB) to {MAX_BYTES_BILLED_LIMIT} (1 TiB)"
        )
    return value


def load_credentials(config: Any) -> BigQueryCredentials:
    """The credentials in ``config`` (the tenant's blob), or
    :class:`BigQueryNotConfigured`. Pure: no network, no environment."""
    if not isinstance(config, Mapping):
        raise BigQueryNotConfigured("no BigQuery credentials are configured")
    info = _key_info(config.get("service_account_json"))
    project = config.get("project_id")
    if not isinstance(project, str) or not PROJECT_ID.match(project):
        raise BigQueryNotConfigured(
            "project_id is missing or is not a Google Cloud project id"
        )
    return BigQueryCredentials(
        project_id=project,
        location=_location(config.get("location")),
        max_bytes_billed=_ceiling(config.get("max_bytes_billed")),
        client_email=str(info["client_email"]).strip(),
        info=info,
    )


#: Why a token wasn't issued (:attr:`BigQueryTokenError.kind`).
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


class BigQueryTokenError(Exception):
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


def forget_token(creds: BigQueryCredentials, token: str | None) -> None:
    """Drop ``token`` from the cache if it is still the one kept for this
    key: BigQuery rejected it, so no later call may reuse it. A newer token
    another call minted meanwhile stays."""
    if not token:
        return
    with _tokens_lock:
        cached = _tokens.get(creds.cache_key)
        if cached is not None and cached[0] == token:
            del _tokens[creds.cache_key]


def access_token(creds: BigQueryCredentials, session: Any, *, timeout: float) -> str:
    """A read-only access token for the key in ``creds`` (see
    :func:`obtain_token`)."""
    return obtain_token(creds, session, timeout=timeout)[0]


def obtain_token(
    creds: BigQueryCredentials, session: Any, *, timeout: float
) -> tuple[str, bool]:
    """``(token, from_cache)``: a cached token while it has more than five
    minutes left, else a new one minted through ``session`` (the transport
    every BigQuery call uses) with one POST bounded by ``timeout``.

    Raises :class:`BigQueryNotConfigured` when the key can't sign (wrong key
    type, corrupt PEM: the exception type only), and
    :class:`BigQueryTokenError` when Google issues no token."""
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


def _assertion(creds: BigQueryCredentials, now: float) -> str:
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
        raise BigQueryNotConfigured(
            "service_account_json's private key can't be loaded or used to sign "
            f"(an RSA service account key is needed; {type(e).__name__})"
        ) from None
    return signed.decode() if isinstance(signed, bytes) else str(signed)


def _exchange(
    creds: BigQueryCredentials,
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
            raise BigQueryTokenError(
                "Google's token endpoint did not answer in time",
                kind=TOKEN_TIMED_OUT,
            ) from None
        raise BigQueryTokenError(
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
            raise BigQueryTokenError(
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
        raise BigQueryTokenError(
            "Google refused the token request's issue and expiry times: this "
            f"host's clock is off (the key is fine; check NTP){said}",
            kind=TOKEN_CLOCK_SKEW,
            status=status,
        )
    if status in (400, 401, 403) and error not in _TRANSIENT_ERRORS:
        raise BigQueryTokenError(
            f"Google did not issue a token for {who}{said}",
            kind=TOKEN_REFUSED,
            status=status,
        )
    if status == 429:
        raise BigQueryTokenError(
            f"Google's token endpoint is rate limiting ({status}){said}",
            kind=TOKEN_RATE_LIMITED,
            status=status,
            retry_after=retry_after,
        )
    if 300 <= status < 400:
        raise BigQueryTokenError(
            f"Google's token endpoint answered a redirect ({status}); redirects are "
            "not followed",
            kind=TOKEN_UPSTREAM,
            status=status,
        )
    raise BigQueryTokenError(
        f"Google's token endpoint failed ({status}){said}",
        kind=TOKEN_UPSTREAM,
        status=status,
        retry_after=retry_after if status == 503 else None,
    )


__all__ = [
    "DEFAULT_MAX_BYTES_BILLED",
    "MAX_BYTES_BILLED_LIMIT",
    "MIN_BYTES_BILLED",
    "PROJECT_ID",
    "READ_ONLY_SCOPE",
    "SERVER_KEYS",
    "TOKEN_CLOCK_SKEW",
    "TOKEN_RATE_LIMITED",
    "TOKEN_REFUSED",
    "TOKEN_TIMED_OUT",
    "TOKEN_UNREACHABLE",
    "TOKEN_UPSTREAM",
    "BigQueryCredentials",
    "BigQueryNotConfigured",
    "BigQueryTokenError",
    "access_token",
    "clear_token_cache",
    "forget_token",
    "load_credentials",
    "obtain_token",
]
