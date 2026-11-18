"""Snowflake credentials: the tenant's blob, checked before any request.

The blob is one JSON object (the README gives the secret's name)::

    {"account": "myorg-myaccount", "user": "AISQUARE_READER", "auth": "keypair",
     "private_key_pem": "-----BEGIN PRIVATE KEY-----\\n...",
     "private_key_passphrase": "<only for an encrypted key>",
     "role": "AISQUARE_READONLY", "warehouse": "AISQUARE_WH",
     "database": "POLICIES", "schema": "PUBLIC"}

    {"account": "myorg-myaccount", "auth": "oauth_client",
     "client_id": "...", "client_secret": "...",
     "token_url": "https://login.microsoftonline.com/<tenant-id>/oauth2/v2.0/token",
     "scope": "api://<app-id>/.default",
     "role": "AISQUARE_READONLY", "warehouse": "AISQUARE_WH"}

* ``account`` is a Snowflake account identifier, never a URL or a host:
  ``<orgname>-<account_name>`` (Snowflake's preferred form), or a legacy
  account locator with its region and cloud (``xy12345``,
  ``xy12345.us-east-2.aws``), either one optionally ``.privatelink``. Every
  request goes to ``https://<account>.snowflakecomputing.com`` (an underscore
  in the account name is written ``-`` in the host, as Snowflake
  recommends). Nothing a caller sends can change it.
* ``auth`` ``keypair`` (the default, for service use) needs ``user`` and
  ``private_key_pem``: an RSA key of at least 2048 bits, PKCS#8 or PKCS#1,
  encrypted with ``private_key_passphrase`` or not. Each client signs a
  key-pair JWT with it (RS256, valid 59 minutes, Snowflake's ``iss`` and
  ``sub``: the account and user upper-cased, and the public key's SHA-256
  fingerprint).
* ``auth`` ``oauth_client`` needs ``client_id``, ``client_secret`` and
  ``token_url``: an OAuth 2.0 client-credentials grant
  (``client_secret_post``) at the customer's identity provider (Okta,
  Microsoft Entra ID, ...), whose tokens Snowflake trusts through an
  External OAuth security integration. Snowflake itself does not issue
  client-credentials tokens. ``scope`` is sent when set. The token is cached
  in memory until five minutes before it expires.
* ``role`` and ``warehouse`` are required (the role holds read-only grants;
  the warehouse is what the statements run, and bill, on). ``database`` and
  ``schema`` are optional defaults for names written without them. All four
  are written as in SQL: unquoted is case-insensitive (``aisquare_wh`` is
  ``AISQUARE_WH``), double quotes keep the case (``"Policies"``).

Anything missing or malformed raises :class:`SnowflakeNotConfigured` with a
reason that never contains a secret, and no request is made. Ambient
configuration (``SNOWFLAKE_*`` environment variables,
``~/.snowflake/connections.toml``, ``~/.snowsql/config``) is never read.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import requests
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from aisquare_pipe_snowflake._http import retry_after_seconds, timed_out
from aisquare_pipe_snowflake.refs import BadReference, parse_identifier

#: ``<orgname>-<account_name>``: the organization name is letters and
#: digits, the account name letters, digits and underscores (lower-cased
#: before matching).
_ORG_ACCOUNT = r"[a-z0-9]{1,64}-[a-z0-9_]{1,255}"
#: A legacy account locator, optionally ``.<region>`` and ``.<cloud>``
#: (``xy12345.us-east-2.aws``, ``xy12345.us-central1.gcp``).
_LOCATOR = r"[a-z][a-z0-9]{1,31}(?:\.[a-z0-9]+(?:-[a-z0-9]+)*){0,2}"
ACCOUNT = re.compile(rf"^(?:{_ORG_ACCOUNT}|{_LOCATOR})(?:\.privatelink)?$")
HOST_SUFFIX = ".snowflakecomputing.com"
AUTH_KEYPAIR = "keypair"
AUTH_OAUTH_CLIENT = "oauth_client"
AUTH_KINDS = (AUTH_KEYPAIR, AUTH_OAUTH_CLIENT)
#: A user name (it goes upper-cased into the JWT's claims): no control
#: character, no double quote and no dot.
_USER_NAME = re.compile(r'^[^\x00-\x1f\x7f".]{1,255}$')
#: A DNS host name whose last label is alphabetic: never an IP literal.
_TOKEN_HOST = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)
#: Snowflake accepts a key-pair JWT for at most an hour; 59 minutes, as in
#: its own sample.
JWT_LIFETIME_SECONDS = 59 * 60
MIN_RSA_BITS = 2048
_REFRESH_SKEW_SECONDS = 300.0
_DEFAULT_TOKEN_LIFETIME = 3600.0
_MAX_TOKEN_CHARS = 16_384

#: The blob's keys: server-controlled, so a caller may set none of them.
#: (``database`` and ``schema`` are defaults a tool argument or pull param
#: may name otherwise: the role's grants, not these, bound what is read.)
CREDENTIAL_KEYS = frozenset(
    {
        "account",
        "user",
        "auth",
        "private_key_pem",
        "private_key_passphrase",
        "client_id",
        "client_secret",
        "token_url",
        "scope",
        "role",
        "warehouse",
    }
)
SERVER_KEYS = CREDENTIAL_KEYS

# Why the token endpoint issued no token.
TOKEN_REFUSED = "refused"  # it refused the client (400/401/403): fix the secret
TOKEN_TIMED_OUT = "timed_out"
TOKEN_RATE_LIMITED = "rate_limited"
TOKEN_UNAVAILABLE = "unavailable"  # unreachable, 5xx or a malformed answer


class SnowflakeNotConfigured(ValueError):
    """The Snowflake credential blob is missing or unusable. The message
    names what is wrong and never includes a secret."""


class SnowflakeTokenError(Exception):
    """The OAuth token endpoint issued no token. ``kind`` says why (one of
    the ``TOKEN_*`` values)."""

    def __init__(
        self, message: str, *, kind: str, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.retry_after = retry_after


@dataclass(frozen=True)
class SnowflakeCredentials:
    account: str
    auth: str
    role: str
    warehouse: str
    user: str | None = None
    database: str | None = None
    schema: str | None = None
    private_key: Any = field(default=None, repr=False, compare=False)
    fingerprint: str | None = None
    client_id: str | None = None
    client_secret: str | None = field(default=None, repr=False)
    token_url: str | None = None
    scope: str | None = None

    @property
    def host(self) -> str:
        return self.account.replace("_", "-") + HOST_SUFFIX

    @property
    def base_url(self) -> str:
        return f"https://{self.host}"

    @property
    def jwt_account(self) -> str:
        """The account as a key-pair JWT names it: upper-cased, without a
        locator's region, cloud or ``privatelink`` (Snowflake's rule and
        sample code): ``MYORG-MY_ACCOUNT``, ``XY12345``."""
        return self.account.upper().split(".", 1)[0]

    @property
    def token_type(self) -> str:
        """``X-Snowflake-Authorization-Token-Type`` for the bearer sent."""
        return "KEYPAIR_JWT" if self.auth == AUTH_KEYPAIR else "OAUTH"

    @property
    def secrets(self) -> tuple[str, ...]:
        """The blob's secret values, for redaction."""
        return tuple(s for s in (self.client_secret,) if s)

    @property
    def cache_key(self) -> str:
        material = (
            f"{self.token_url}\0{self.client_id}\0{self.client_secret}\0{self.scope}"
        )
        return hashlib.sha256(material.encode()).hexdigest()


def _secret(config: Mapping[str, Any], key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SnowflakeNotConfigured(f"{key} is missing")
    value = value.strip()
    if any(ch.isspace() for ch in value) or not value.isprintable():
        raise SnowflakeNotConfigured(f"{key} holds whitespace or control characters")
    return value


def _name(config: Mapping[str, Any], key: str, *, required: bool) -> str | None:
    """A role, warehouse, database or schema, written as in SQL (unquoted:
    upper-cased; in double quotes: exact) and resolved."""
    value = config.get(key)
    if value is None or value == "":
        if required:
            raise SnowflakeNotConfigured(f"{key} is missing")
        return None
    try:
        return parse_identifier(value, key)
    except BadReference:
        raise SnowflakeNotConfigured(
            f"{key} must be one Snowflake object name (unquoted, or in double quotes)"
        ) from None


def _user(config: Mapping[str, Any]) -> str:
    value = config.get("user")
    if not isinstance(value, str) or not value.strip():
        raise SnowflakeNotConfigured(
            "user is missing (the Snowflake user the key belongs to)"
        )
    if not _USER_NAME.match(value.strip()):
        raise SnowflakeNotConfigured("user must be a Snowflake user name")
    return value.strip()


def _account(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SnowflakeNotConfigured(
            "account is missing (a Snowflake account identifier, e.g. myorg-myaccount)"
        )
    account = value.strip().lower()
    if not ACCOUNT.match(account):
        raise SnowflakeNotConfigured(
            "account must be a Snowflake account identifier (myorg-myaccount, or an account "
            "locator such as xy12345.us-east-2.aws), not a URL or a host"
        )
    return account


#: Parsed keys by the SHA-256 of PEM and passphrase: loading (and validating)
#: an RSA key is slow, and every call reads the blob. In memory only.
_keys: dict[bytes, tuple[Any, str]] = {}
_keys_lock = threading.Lock()
_MAX_KEYS = 64


def _private_key(config: Mapping[str, Any]) -> tuple[Any, str]:
    pem = config.get("private_key_pem")
    if not isinstance(pem, str) or "PRIVATE KEY" not in pem:
        raise SnowflakeNotConfigured(
            "private_key_pem is missing (a PEM RSA private key)"
        )
    if "\\n" in pem and "\n" not in pem.strip():
        pem = pem.replace("\\n", "\n")  # pasted with its newlines escaped
    passphrase = config.get("private_key_passphrase")
    if passphrase is None or passphrase == "":
        password = None
    elif isinstance(passphrase, str):
        password = passphrase.encode()
    else:
        raise SnowflakeNotConfigured("private_key_passphrase must be a string")
    digest = hashlib.sha256(pem.strip().encode() + b"\0" + (password or b"")).digest()
    with _keys_lock:
        known = _keys.get(digest)
    if known is not None:
        return known
    try:
        key = serialization.load_pem_private_key(
            pem.strip().encode(), password=password
        )
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise SnowflakeNotConfigured(
            "private_key_pem is not a usable private key, or private_key_passphrase does "
            "not open it"
        ) from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise SnowflakeNotConfigured(
            "private_key_pem must be an RSA key (Snowflake key-pair authentication)"
        )
    if key.key_size < MIN_RSA_BITS:
        raise SnowflakeNotConfigured(
            f"private_key_pem must be at least {MIN_RSA_BITS} bits"
        )
    der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(der).digest()).decode()
    with _keys_lock:
        if len(_keys) >= _MAX_KEYS:
            _keys.pop(next(iter(_keys)))
        _keys[digest] = (key, fingerprint)
    return key, fingerprint


def _token_url(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SnowflakeNotConfigured("token_url is missing (the OAuth token endpoint)")
    text = value.strip()
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise SnowflakeNotConfigured("token_url is not a URL") from None
    host = (parts.hostname or "").lower()
    if (
        parts.scheme != "https"
        or port is not None
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or not text.isprintable()
        or any(ch.isspace() for ch in text)
        or not _TOKEN_HOST.match(host)
        or host == "localhost"
        or host.endswith(".localhost")
    ):
        raise SnowflakeNotConfigured(
            "token_url must be an https URL on a DNS host name (no IP address, port, "
            "userinfo or fragment)"
        )
    return text


def load_credentials(config: Any) -> SnowflakeCredentials:
    """The credentials in ``config`` (the tenant's blob), or
    :class:`SnowflakeNotConfigured`. Pure: no network, no environment."""
    if not isinstance(config, Mapping):
        raise SnowflakeNotConfigured("no Snowflake credentials are configured")
    account = _account(config.get("account"))
    auth = config.get("auth")
    auth = AUTH_KEYPAIR if auth is None or auth == "" else auth
    if auth not in AUTH_KINDS:
        raise SnowflakeNotConfigured("auth must be 'keypair' or 'oauth_client'")
    role = _name(config, "role", required=True)
    warehouse = _name(config, "warehouse", required=True)
    assert role is not None and warehouse is not None
    common: dict[str, Any] = {
        "account": account,
        "auth": auth,
        "role": role,
        "warehouse": warehouse,
        "database": _name(config, "database", required=False),
        "schema": _name(config, "schema", required=False),
    }
    if auth == AUTH_KEYPAIR:
        user = _user(config)
        key, fingerprint = _private_key(config)
        return SnowflakeCredentials(
            user=user, private_key=key, fingerprint=fingerprint, **common
        )
    scope = config.get("scope")
    if scope is None or scope == "":
        scope = None
    elif (
        not isinstance(scope, str)
        or not scope.strip()
        or not scope.strip().isprintable()
    ):
        raise SnowflakeNotConfigured("scope must be a string")
    return SnowflakeCredentials(
        client_id=_secret(config, "client_id"),
        client_secret=_secret(config, "client_secret"),
        token_url=_token_url(config.get("token_url")),
        scope=scope.strip() if isinstance(scope, str) else None,
        **common,
    )


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def keypair_jwt(creds: SnowflakeCredentials, *, now: float | None = None) -> str:
    """A key-pair JWT for the SQL API: RS256, ``iss``
    ``<ACCOUNT>.<USER>.SHA256:<fingerprint>``, ``sub`` ``<ACCOUNT>.<USER>``,
    valid :data:`JWT_LIFETIME_SECONDS`."""
    if creds.auth != AUTH_KEYPAIR or creds.private_key is None or not creds.user:
        raise SnowflakeNotConfigured(
            "key-pair authentication needs user and private_key_pem"
        )
    issued = int(time.time() if now is None else now)
    qualified = f"{creds.jwt_account}.{creds.user.upper()}"
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {
        "iss": f"{qualified}.{creds.fingerprint}",
        "sub": qualified,
        "iat": issued,
        "exp": issued + JWT_LIFETIME_SECONDS,
    }
    signing_input = ".".join(
        _b64url(json.dumps(part, separators=(",", ":")).encode())
        for part in (header, claims)
    )
    signature = creds.private_key.sign(
        signing_input.encode(), padding.PKCS1v15(), hashes.SHA256()
    )
    return f"{signing_input}.{_b64url(signature)}"


_tokens: dict[str, tuple[str, float]] = {}
_tokens_lock = threading.Lock()


def clear_token_cache() -> None:
    """Forget every OAuth token minted (tests)."""
    with _tokens_lock:
        _tokens.clear()


def forget_token(creds: SnowflakeCredentials, token: str | None) -> None:
    """Drop ``token`` from the cache (Snowflake refused it)."""
    if token is None or creds.auth != AUTH_OAUTH_CLIENT:
        return
    with _tokens_lock:
        cached = _tokens.get(creds.cache_key)
        if cached is not None and cached[0] == token:
            del _tokens[creds.cache_key]


def bearer_token(
    creds: SnowflakeCredentials, session: Any, *, timeout: float
) -> tuple[str, bool]:
    """``(bearer, from_cache)`` for the SQL API: a fresh key-pair JWT, or
    an OAuth client-credentials token (cached, or minted now with ONE POST
    to ``token_url``; no hidden retries). Raises :class:`SnowflakeTokenError`
    when the endpoint issues none."""
    if creds.auth == AUTH_KEYPAIR:
        return keypair_jwt(creds), False
    now = time.time()
    with _tokens_lock:
        cached = _tokens.get(creds.cache_key)
        if cached is not None and cached[1] - _REFRESH_SKEW_SECONDS > now:
            return cached[0], True
    form = {
        "grant_type": "client_credentials",
        "client_id": creds.client_id or "",
        "client_secret": creds.client_secret or "",
    }
    if creds.scope:
        form["scope"] = creds.scope
    try:
        resp = session.request(
            "POST",
            creds.token_url,
            data=form,
            headers={"Accept": "application/json"},
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.RequestException as e:
        if timed_out(e):
            raise SnowflakeTokenError(
                "the OAuth token endpoint did not answer in time", kind=TOKEN_TIMED_OUT
            ) from None
        raise SnowflakeTokenError(
            f"could not reach the OAuth token endpoint ({type(e).__name__})",
            kind=TOKEN_UNAVAILABLE,
        ) from None
    try:
        status = resp.status_code
        if status == 429:
            raise SnowflakeTokenError(
                "the OAuth token endpoint is rate limiting this client (429)",
                kind=TOKEN_RATE_LIMITED,
                retry_after=retry_after_seconds(
                    (resp.headers or {}).get("Retry-After")
                ),
            )
        if status in (400, 401, 403):
            raise SnowflakeTokenError(
                f"the OAuth token endpoint refused the client credentials ({status}): the "
                "client id or secret is wrong, expired, or not allowed the client "
                "credentials grant",
                kind=TOKEN_REFUSED,
            )
        if status != 200:
            raise SnowflakeTokenError(
                f"the OAuth token endpoint failed ({status})",
                kind=TOKEN_UNAVAILABLE,
                retry_after=retry_after_seconds((resp.headers or {}).get("Retry-After"))
                if status == 503
                else None,
            )
        try:
            body = resp.json()
        except ValueError:
            body = None
    finally:
        resp.close()
    token = body.get("access_token") if isinstance(body, dict) else None
    if (
        not isinstance(token, str)
        or not token
        or len(token) > _MAX_TOKEN_CHARS
        or not token.isprintable()
        or any(ch.isspace() for ch in token)
    ):
        raise SnowflakeTokenError(
            "the OAuth token endpoint answered without a usable access_token",
            kind=TOKEN_UNAVAILABLE,
        )
    expires_in = body.get("expires_in") if isinstance(body, dict) else None
    try:
        lifetime = (
            float(expires_in) if expires_in is not None else _DEFAULT_TOKEN_LIFETIME
        )
    except (TypeError, ValueError):
        lifetime = _DEFAULT_TOKEN_LIFETIME
    if not 0 < lifetime <= 86_400:
        lifetime = _DEFAULT_TOKEN_LIFETIME
    with _tokens_lock:
        _tokens[creds.cache_key] = (token, now + lifetime)
    return token, False


__all__ = [
    "ACCOUNT",
    "AUTH_KEYPAIR",
    "AUTH_KINDS",
    "AUTH_OAUTH_CLIENT",
    "CREDENTIAL_KEYS",
    "JWT_LIFETIME_SECONDS",
    "SERVER_KEYS",
    "TOKEN_RATE_LIMITED",
    "TOKEN_REFUSED",
    "TOKEN_TIMED_OUT",
    "TOKEN_UNAVAILABLE",
    "SnowflakeCredentials",
    "SnowflakeNotConfigured",
    "SnowflakeTokenError",
    "bearer_token",
    "clear_token_cache",
    "forget_token",
    "keypair_jwt",
    "load_credentials",
]
