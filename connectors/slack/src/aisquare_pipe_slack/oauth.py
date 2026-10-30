"""Slack OAuth v2: one consent can mint a user token, a bot token, or both.

The authorize URL carries user scopes in ``user_scope`` and bot scopes in
``scope``. ``oauth.v2.access`` answers with the bot token at the top level
(``token_type: "bot"``) and the user token under ``authed_user``; a
user-token-only answer may also carry it at the top level
(``token_type: "user"``). :func:`parse_installation` accepts every shape.

Slack answers errors as HTTP 200 with ``{"ok": false, "error": ...}``;
those become :class:`~aisquare.pipe.OAuthError` with Slack's code, except
``invalid_refresh_token``, which is reported as ``invalid_grant`` so a host
treats it as a dead grant (reconnect) rather than a transient failure.

PKCE stays off: turning it on in Slack marks the app a public client for good
(only Slack support can undo it) and caps refresh tokens at 30 days.
"""

from __future__ import annotations

import base64
import logging
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import requests
from aisquare.pipe import OAuthError, OAuthSpec, OAuthToken

from aisquare_pipe_slack.constants import API_BASE, TOKEN_ROTATION, USER_AGENT
from aisquare_pipe_slack.rest import (
    SAFE_CODE,
    SlackAPIError,
    SlackWebClient,
    parse_body,
)
from aisquare_pipe_slack.scopes import USER_SCOPES

logger = logging.getLogger("aisquare.pipe.slack")

DEFAULT_TIMEOUT = 30.0

#: Slack's OAuth v2. ``default_scopes`` (every user scope) is informational:
#: :meth:`SlackToolkit.build_authorize_url` places scopes itself. Revocation is
#: Slack's ``auth.revoke``, not RFC 7009 — see :func:`revoke_user_token`.
SLACK_OAUTH = OAuthSpec(
    provider="slack",
    authorize_url="https://slack.com/oauth/v2/authorize",
    token_url=f"{API_BASE}/oauth.v2.access",
    default_scopes=USER_SCOPES,
    scope_separator=",",
    pkce=False,
    # Rotation on: 12 h access tokens, and every refresh returns a new refresh
    # token (persist it). Off: tokens never expire and there is no refresh token.
    token_expires=TOKEN_ROTATION,
    rotates_refresh_token=TOKEN_ROTATION,
    # Slack's oauth.v2.access recommends HTTP Basic for the client
    # credentials ("POST parameters are possible but discouraged").
    token_auth_method="client_secret_basic",
)

#: Slack codes reported as OAuth's ``invalid_grant`` (a dead grant).
_DEAD_GRANT = frozenset({"invalid_refresh_token", "invalid_grant"})


@dataclass(frozen=True)
class SlackInstallation:
    """What one consent produced: the user token, the bot token (either may
    be None, depending on the scopes asked for), and who/where it is for."""

    user_token: OAuthToken | None
    bot_token: OAuthToken | None
    team_id: str | None = None
    enterprise_id: str | None = None
    user_id: str | None = None
    bot_user_id: str | None = None
    app_id: str | None = None


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _id_of(value: Any) -> str | None:
    return _string(value.get("id")) if isinstance(value, Mapping) else None


def parse_installation(payload: Mapping[str, Any]) -> SlackInstallation:
    """Both tokens from an ``oauth.v2.access`` (or ``oauth.v2.user.access``)
    answer. Raises :class:`OAuthError` when it carries no token at all."""
    user_token = bot_token = None
    authed = payload.get("authed_user")
    if isinstance(authed, Mapping) and _string(authed.get("access_token")):
        user_token = OAuthToken.from_response(authed, provider="slack")
    top = _string(payload.get("access_token"))
    if top:
        token = OAuthToken.from_response(payload, provider="slack")
        if payload.get("token_type") == "user":
            user_token = user_token or token
        else:
            bot_token = token
    if user_token is None and bot_token is None:
        raise OAuthError(
            "slack: token response has no access_token", provider="slack", error="invalid_token_response"
        )
    return SlackInstallation(
        user_token=user_token,
        bot_token=bot_token,
        team_id=_id_of(payload.get("team")),
        enterprise_id=_id_of(payload.get("enterprise")),
        user_id=_id_of(authed) or _string(payload.get("user_id")),
        bot_user_id=_string(payload.get("bot_user_id")),
        app_id=_string(payload.get("app_id")),
    )


def _basic_auth(client_id: str, client_secret: str) -> str:
    """RFC 6749 §2.3.1 HTTP Basic: both parts form-encoded, then base64."""
    raw = f"{urllib.parse.quote(client_id, safe='')}:{urllib.parse.quote(client_secret, safe='')}"
    return "Basic " + base64.b64encode(raw.encode("utf-8")).decode("ascii")


def token_request(
    form: Mapping[str, str],
    *,
    client_id: str,
    client_secret: str | None,
    step: str,
    session: requests.Session | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """POST to ``oauth.v2.access`` and return the payload, or raise
    :class:`OAuthError` with Slack's error code. The client credentials go
    as HTTP Basic, which Slack recommends (``client_id`` in the body only
    for a client without a secret); redirects are never followed."""
    body = dict(form)
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if client_secret is None:
        body["client_id"] = client_id
    else:
        headers["Authorization"] = _basic_auth(client_id, client_secret)
    http = session if session is not None else requests.Session()
    try:
        response = http.request(
            "POST",
            SLACK_OAUTH.token_url,
            data=body,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.RequestException as e:
        raise OAuthError(
            f"slack: token {step} failed — {type(e).__name__}", provider="slack", error="network_error"
        ) from None
    finally:
        if session is None:
            http.close()
    status = response.status_code
    if 300 <= status < 400:
        raise OAuthError(
            f"slack: token {step} failed — the token endpoint answered HTTP {status} "
            "(redirects are not followed)",
            provider="slack",
            error="unexpected_redirect",
            status=status,
        )
    payload = parse_body(response)
    if not isinstance(payload, dict):
        raise OAuthError(
            f"slack: token {step} failed — the response is not JSON (HTTP {status})",
            provider="slack",
            error="invalid_token_response",
            status=status,
        )
    error = payload.get("error")
    if status >= 400 or payload.get("ok") is False or error:
        if isinstance(error, str) and SAFE_CODE.fullmatch(error):
            code = error
        else:
            code = f"http_{status}" if status >= 400 else "unknown_error"
        if code in _DEAD_GRANT:
            code = "invalid_grant"
        raise OAuthError(
            f"slack: token {step} failed ({code})", provider="slack", error=code, status=status
        )
    return payload


def revoke_user_token(
    token: str, *, session: requests.Session | None = None, timeout: float = DEFAULT_TIMEOUT
) -> bool:
    """Revoke one user token with ``auth.revoke``. A bot token is refused
    (False, nothing revoked): it is shared by everyone in the workspace, so
    one person disconnecting must not switch the app's bot off for all of
    them. ``auth.test`` decides which kind the token is — prefixes have
    changed before. Only this token dies; ``apps.uninstall`` is never called.
    Both calls go through :class:`~aisquare_pipe_slack.rest.SlackWebClient`,
    without retries. True only for ``ok`` + ``revoked``. Never raises."""
    try:
        with SlackWebClient(token, session=session, timeout=timeout, max_retries=0) as client:
            who = client.call("auth.test", retry=False)
            if who.get("bot_id"):
                logger.warning("slack: token revoke refused for a bot token (shared by the workspace)")
                return False
            answer = client.call("auth.revoke", retry=False)
    except SlackAPIError as e:
        logger.warning("slack: token revoke failed (%s)", e.error)
        return False
    except Exception as e:  # noqa: BLE001 — never raises (e.g. a non-latin-1 token in a header)
        logger.warning("slack: token revoke failed (%s)", type(e).__name__)
        return False
    if answer.get("revoked") is True:
        return True
    logger.warning("slack: auth.revoke did not revoke the token")
    return False
