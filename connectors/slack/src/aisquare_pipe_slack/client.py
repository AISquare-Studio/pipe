"""Thin Slack Web API client.

Domain-pure: config in, API payloads out. ``session`` is injectable so tests
never touch the network.

Unlike most REST APIs, Slack signals application errors as HTTP 200 with
``{"ok": false, "error": "<code>"}`` — ``_payload_or_raise`` folds both the
transport status and the ``ok`` field onto one error hierarchy.
"""

from __future__ import annotations

from typing import Any

import requests

API_BASE_URL = "https://slack.com/api"
DEFAULT_TIMEOUT = 30

# Slack error codes that mean the token (or its scopes) is the problem.
AUTH_ERROR_CODES = frozenset(
    {
        "invalid_auth",
        "not_authed",
        "token_revoked",
        "token_expired",
        "account_inactive",
        "missing_scope",
    }
)


class SlackError(RuntimeError):
    def __init__(self, status_code: int, error: str = "", body: str = ""):
        self.status_code = status_code
        self.error = error
        self.body = body[:512]
        super().__init__(f"Slack returned {status_code}: {error or self.body}")


class SlackAuthError(SlackError):
    """Token invalid/revoked or a required scope is missing."""


class SlackRateLimited(SlackError):
    """HTTP 429 or ``ok:false error:ratelimited`` — retry with backoff."""

    def __init__(self, status_code: int, error: str = "", body: str = "", retry_after: int = 0):
        self.retry_after = retry_after
        super().__init__(status_code, error, body)


def _payload_or_raise(response: requests.Response) -> dict[str, Any]:
    if response.status_code == 429:
        raise SlackRateLimited(
            429,
            "ratelimited",
            response.text,
            retry_after=int(response.headers.get("Retry-After", 0) or 0),
        )
    if response.status_code in (401, 403):
        raise SlackAuthError(response.status_code, "", response.text)
    if not 200 <= response.status_code < 300:
        raise SlackError(response.status_code, "", response.text)
    try:
        payload = response.json()
    except ValueError as exc:
        raise SlackError(response.status_code, "invalid_json", response.text) from exc
    if not payload.get("ok", False):
        error = str(payload.get("error", ""))
        if error in AUTH_ERROR_CODES:
            raise SlackAuthError(response.status_code, error, response.text)
        if error == "ratelimited":
            raise SlackRateLimited(response.status_code, error, response.text)
        raise SlackError(response.status_code, error, response.text)
    return payload


class SlackClient:
    """Config keys: ``bot_token`` (``xoxb-…``)."""

    def __init__(self, config: dict[str, Any], session: requests.Session | None = None):
        self._config = config
        self._session = session or requests.Session()

    def validate(self) -> bool:
        return bool(self._config.get("bot_token"))

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._config['bot_token']}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._session.post(
            f"{API_BASE_URL}/{method}",
            headers=self._headers(),
            json={k: v for k, v in payload.items() if v not in (None, "", [])},
            timeout=DEFAULT_TIMEOUT,
        )
        return _payload_or_raise(response)

    def auth_test(self) -> dict[str, Any]:
        """Identify the token: ``team_id`` / ``team`` / ``bot_id`` / ``user_id``."""
        return self._post("auth.test", {})

    def post_message(
        self,
        channel: str,
        text: str = "",
        blocks: list[dict[str, Any]] | None = None,
        thread_ts: str = "",
        username: str = "",
        icon_emoji: str = "",
    ) -> dict[str, Any]:
        """``chat.postMessage`` — returns the payload; ``ts`` is the message ref."""
        return self._post(
            "chat.postMessage",
            {
                "channel": channel,
                "text": text,
                "blocks": blocks or [],
                "thread_ts": thread_ts,
                "username": username,
                "icon_emoji": icon_emoji,
            },
        )

    def post_ephemeral(
        self,
        channel: str,
        user: str,
        text: str = "",
        blocks: list[dict[str, Any]] | None = None,
        thread_ts: str = "",
    ) -> dict[str, Any]:
        """``chat.postEphemeral`` — visible only to ``user``; ref is ``message_ts``."""
        return self._post(
            "chat.postEphemeral",
            {
                "channel": channel,
                "user": user,
                "text": text,
                "blocks": blocks or [],
                "thread_ts": thread_ts,
            },
        )
