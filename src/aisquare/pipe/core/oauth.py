"""OAuth 2.0 authorization-code flow primitives for toolkits.

A toolkit declares *how* its provider does OAuth with an :class:`OAuthSpec`;
the functions here run the flow steps (authorize URL, code exchange, refresh,
revoke) over stdlib ``urllib`` so core keeps zero runtime dependencies.
Callers own the client credentials and the token storage — nothing here
persists or logs secrets.

Provider quirks handled generically:

* ``Accept: application/json`` is always sent, but form-encoded bodies are
  parsed too (GitHub answers form-encoded without it, and on some errors).
* An ``error`` key in a 200 response is an error (GitHub does this).
* A refresh response without a ``refresh_token`` keeps the old one; a
  response with one (rotation) replaces it. A missing ``scope`` falls back
  to the requested or previous scope.

Transport safety: endpoints must be ``https`` (plain ``http`` only for
loopback hosts, so tests can run local fakes), and redirects from the
token/revoke endpoints are never followed — a 3xx is an error, so client
credentials can't be forwarded to another origin.

Anything more exotic belongs in a ``Toolkit`` override of the matching
method (``exchange_code`` / ``refresh_access_token`` / ``revoke_token``).
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import logging
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from aisquare.pipe.errors import OAuthError

logger = logging.getLogger("aisquare.pipe")

DEFAULT_TIMEOUT = 30.0

#: Longest provider ``error_description`` carried into an OAuthError message.
_MAX_DESCRIPTION = 300

#: Hosts allowed over plain http (local fakes in tests); everything else needs https.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

#: Authorize-URL parameters the flow owns; spec extras and params baked
#: into ``authorize_url`` can never override them.
FLOW_AUTHORIZE_PARAMS = frozenset(
    {"response_type", "client_id", "redirect_uri", "state", "code_challenge", "code_challenge_method"}
)


@dataclass(frozen=True)
class OAuthSpec:
    """How a provider does OAuth 2.0 (authorization-code grant).

    ``token_auth_method`` picks how client credentials reach the token
    endpoint: in the form body (``client_secret_post``, the common case) or
    as an HTTP Basic header (``client_secret_basic``).
    ``extra_authorize_params`` adds provider-specific authorize parameters
    (e.g. ``access_type=offline``); it cannot override the flow's own
    (:data:`FLOW_AUTHORIZE_PARAMS`).

    ``token_expires`` and ``rotates_refresh_token`` are informational hints
    for the host's token store — the flow functions don't consult them:

    * ``token_expires=False`` (e.g. GitHub OAuth apps): access tokens live
      until revoked; ``expires_at`` stays ``None`` and there is nothing to
      refresh on a schedule.
    * ``rotates_refresh_token=True`` (e.g. Microsoft): every refresh returns
      a new refresh token and invalidates the old one, so persist the
      returned token on every refresh and serialize refreshes per
      connection — two racing refreshes lose the connection.
    """

    provider: str
    authorize_url: str
    token_url: str
    revoke_url: str | None = None
    default_scopes: tuple[str, ...] = ()
    scope_separator: str = " "
    extra_authorize_params: Mapping[str, str] = field(default_factory=dict, hash=False)
    pkce: bool = False
    token_expires: bool = True
    rotates_refresh_token: bool = False
    token_auth_method: Literal["client_secret_post", "client_secret_basic"] = (
        "client_secret_post"
    )

    def __post_init__(self) -> None:
        # Accept a list for convenience; store a tuple so the spec stays immutable.
        object.__setattr__(self, "default_scopes", tuple(self.default_scopes))


@dataclass
class OAuthToken:
    """A token set from a provider's token endpoint.

    ``expires_at`` is epoch seconds (``None`` = the provider did not say /
    the token does not expire). Secrets are excluded from ``repr`` so a
    stray log line cannot leak them; ``to_dict`` does include them — it is
    the storage format.
    """

    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expires_at: int | None = None
    scope: str = ""
    token_type: str = "Bearer"

    def expired(self, skew: int = 60, *, now: float | None = None) -> bool:
        """True when the token expires within ``skew`` seconds (or already has)."""
        if self.expires_at is None:
            return False
        current = time.time() if now is None else now
        return current + skew >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "scope": self.scope,
            "token_type": self.token_type,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> OAuthToken:
        expires_at = data.get("expires_at")
        return cls(
            access_token=str(data["access_token"]),
            refresh_token=data.get("refresh_token") or None,
            expires_at=int(expires_at) if expires_at is not None else None,
            scope=str(data.get("scope") or ""),
            token_type=str(data.get("token_type") or "Bearer"),
        )

    @classmethod
    def from_response(
        cls,
        payload: Mapping[str, Any],
        *,
        provider: str | None = None,
        now: float | None = None,
    ) -> OAuthToken:
        """Build a token from a token-endpoint response body.

        Raises :class:`OAuthError` when the body carries no access token.
        """
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise OAuthError(
                f"{provider or 'oauth'}: token response has no access_token",
                provider=provider,
                error="invalid_token_response",
            )
        expires_at: int | None = None
        expires_in = payload.get("expires_in")
        if expires_in not in (None, ""):
            try:
                current = time.time() if now is None else now
                expires_at = int(current + float(expires_in))
            except (TypeError, ValueError):
                expires_at = None
        scope = payload.get("scope") or ""
        if isinstance(scope, list):
            scope = " ".join(str(s) for s in scope)
        refresh_token = payload.get("refresh_token")
        return cls(
            access_token=access_token,
            refresh_token=refresh_token if isinstance(refresh_token, str) and refresh_token else None,
            expires_at=expires_at,
            scope=str(scope),
            token_type=str(payload.get("token_type") or "Bearer"),
        )


# --------------------------------------------------------------------------
# Flow steps
# --------------------------------------------------------------------------


def pkce_challenge(verifier: str) -> str:
    """The S256 ``code_challenge`` for a ``code_verifier`` (RFC 7636 §4.2)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def make_pkce_pair() -> tuple[str, str]:
    """A fresh ``(code_verifier, code_challenge)`` pair (RFC 7636, S256)."""
    verifier = secrets.token_urlsafe(64)  # 86 chars, inside the 43-128 window
    return verifier, pkce_challenge(verifier)


def build_authorize_url(
    spec: OAuthSpec,
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    scopes: Sequence[str] | None = None,
    code_challenge: str | None = None,
) -> str:
    """The provider URL to send the user to. ``scopes=None`` uses the
    spec's ``default_scopes``. ``state`` (the caller's CSRF token) must be
    non-blank. A PKCE spec requires ``code_challenge`` (from
    :func:`make_pkce_pair`)."""
    _require_secure_url(spec, spec.authorize_url, "authorize_url")
    if not isinstance(state, str) or not state.strip():
        raise OAuthError(
            f"{spec.provider}: a non-empty state is required (CSRF protection)",
            provider=spec.provider,
            error="state_required",
        )
    if spec.pkce and not code_challenge:
        raise OAuthError(
            f"{spec.provider}: PKCE is required — pass code_challenge from make_pkce_pair()",
            provider=spec.provider,
            error="pkce_required",
        )
    # Spec extras first; the flow's own parameters always win.
    params: dict[str, str] = {
        k: v for k, v in spec.extra_authorize_params.items() if k not in FLOW_AUTHORIZE_PARAMS
    }
    scope_list = list(spec.default_scopes if scopes is None else scopes)
    if scope_list:
        params["scope"] = spec.scope_separator.join(scope_list)
    params.update(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"

    # Params baked into authorize_url survive unless the flow owns or sets the key.
    parts = urllib.parse.urlsplit(spec.authorize_url)
    query = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if k not in params and k not in FLOW_AUTHORIZE_PARAMS
    ]
    query += list(params.items())
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def exchange_code(
    spec: OAuthSpec,
    *,
    client_id: str,
    client_secret: str | None,
    code: str,
    redirect_uri: str,
    code_verifier: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> OAuthToken:
    """Trade an authorization code for a token set. A PKCE spec requires
    the ``code_verifier`` whose challenge went into the authorize URL."""
    if spec.pkce and not code_verifier:
        raise OAuthError(
            f"{spec.provider}: PKCE is required — pass the code_verifier from make_pkce_pair()",
            provider=spec.provider,
            error="pkce_required",
        )
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    if code_verifier:
        form["code_verifier"] = code_verifier
    payload = _token_request(spec, form, client_id, client_secret, "exchange", timeout)
    return OAuthToken.from_response(payload, provider=spec.provider)


def refresh_access_token(
    spec: OAuthSpec,
    *,
    client_id: str,
    client_secret: str | None,
    refresh_token: str,
    scopes: Sequence[str] | None = None,
    previous_scope: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> OAuthToken:
    """Mint a fresh access token. Keeps ``refresh_token`` when the provider
    returns none; takes the new one when it rotates. When the response omits
    ``scope`` (RFC 6749 §5.1: unchanged), the token keeps the requested
    ``scopes`` or, failing that, ``previous_scope`` (the stored token's)."""
    form = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    if scopes:
        form["scope"] = spec.scope_separator.join(scopes)
    payload = _token_request(spec, form, client_id, client_secret, "refresh", timeout)
    token = OAuthToken.from_response(payload, provider=spec.provider)
    if token.refresh_token is None:
        token.refresh_token = refresh_token
    if not token.scope:
        token.scope = form.get("scope") or previous_scope or ""
    return token


def revoke_token(
    spec: OAuthSpec,
    *,
    client_id: str,
    client_secret: str | None,
    token: str,
    token_type_hint: str = "access_token",
    timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """Best-effort RFC 7009 revocation. Returns True on a 2xx, False on any
    failure (or when the spec has no ``revoke_url``); never raises."""
    if not spec.revoke_url:
        return False
    form = {"token": token, "token_type_hint": token_type_hint}
    data, headers = _with_client_auth(spec, form, client_id, client_secret)
    try:
        _require_secure_url(spec, spec.revoke_url, "revoke_url")
        status, _ = _http_post_form(spec.revoke_url, data, headers, timeout)
    except Exception as e:  # noqa: BLE001 — best effort by contract
        logger.warning("%s: token revoke failed (%s)", spec.provider, type(e).__name__)
        return False
    if 200 <= status < 300:
        return True
    logger.warning("%s: token revoke returned HTTP %s", spec.provider, status)
    return False


# --------------------------------------------------------------------------
# HTTP plumbing
# --------------------------------------------------------------------------


def _require_secure_url(spec: OAuthSpec, url: str, what: str) -> None:
    """https only; plain http is allowed for loopback hosts (local fakes)."""
    parts = urllib.parse.urlsplit(url if isinstance(url, str) else "")
    host = (parts.hostname or "").lower()
    if host and (parts.scheme == "https" or (parts.scheme == "http" and host in _LOOPBACK_HOSTS)):
        return
    raise OAuthError(
        f"{spec.provider}: {what} must be an https URL",
        provider=spec.provider,
        error="insecure_url",
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: urllib then raises the 3xx as an HTTPError,
    which ``_http_post_form`` returns as a status — credentials never follow
    a ``Location`` to another origin."""

    def redirect_request(self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: Any) -> None:
        return None


def _with_client_auth(
    spec: OAuthSpec, form: dict[str, str], client_id: str, client_secret: str | None
) -> tuple[dict[str, str], dict[str, str]]:
    headers: dict[str, str] = {}
    body = dict(form)
    if spec.token_auth_method == "client_secret_basic" and client_secret is not None:
        raw = f"{urllib.parse.quote(client_id, safe='')}:{urllib.parse.quote(client_secret, safe='')}"
        headers["Authorization"] = "Basic " + base64.b64encode(raw.encode()).decode("ascii")
    else:
        body["client_id"] = client_id
        if client_secret is not None:
            body["client_secret"] = client_secret
    return body, headers


def _token_request(
    spec: OAuthSpec,
    form: dict[str, str],
    client_id: str,
    client_secret: str | None,
    step: str,
    timeout: float,
) -> dict[str, Any]:
    _require_secure_url(spec, spec.token_url, "token_url")
    data, headers = _with_client_auth(spec, form, client_id, client_secret)
    try:
        status, payload = _http_post_form(spec.token_url, data, headers, timeout)
    except (OSError, http.client.HTTPException) as e:
        raise OAuthError(
            f"{spec.provider}: token {step} failed — {type(e).__name__}: {_reason(e)}",
            provider=spec.provider,
            error="network_error",
        ) from None
    if 300 <= status < 400:
        raise OAuthError(
            f"{spec.provider}: token {step} failed — the token endpoint answered HTTP {status} "
            "(redirects are not followed)",
            provider=spec.provider,
            error="unexpected_redirect",
            status=status,
        )
    error = payload.get("error")
    if status >= 400 or error:
        code = str(error) if error else f"http_{status}"
        description = payload.get("error_description")
        description = str(description)[:_MAX_DESCRIPTION] if description else None
        message = f"{spec.provider}: token {step} failed ({code})"
        if description:
            message += f": {description}"
        raise OAuthError(
            message,
            provider=spec.provider,
            error=code,
            description=description,
            status=status,
        )
    return payload


def _reason(e: BaseException) -> str:
    reason = getattr(e, "reason", None)
    return str(reason if reason is not None else e)[:_MAX_DESCRIPTION]


def _http_post_form(
    url: str, form: Mapping[str, str], headers: Mapping[str, str], timeout: float
) -> tuple[int, dict[str, Any]]:
    """POST a form without following redirects; return ``(status, parsed
    body)``. HTTP error and 3xx statuses are returned, not raised; transport
    failures raise ``OSError`` or ``http.client.HTTPException``."""
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(form).encode("ascii"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "aisquare-pipe",
            **headers,
        },
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            status = int(getattr(response, "status", None) or response.getcode())
            body = response.read()
            content_type = response.headers.get("Content-Type", "") if response.headers else ""
    except urllib.error.HTTPError as e:
        status = e.code
        body = e.read() or b""
        content_type = e.headers.get("Content-Type", "") if e.headers else ""
    return status, _parse_body(body, content_type)


def _parse_body(body: bytes, content_type: str) -> dict[str, Any]:
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return {}
    if "json" in content_type or text.startswith("{"):
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    if "=" not in text or text.startswith("<"):
        return {}
    return dict(urllib.parse.parse_qsl(text, keep_blank_values=True))
