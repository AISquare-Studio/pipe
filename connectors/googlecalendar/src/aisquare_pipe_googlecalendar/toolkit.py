"""GoogleCalendarToolkit — Google Calendar's LLM-callable tools plus its Google OAuth spec."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

import requests
from aisquare.pipe import (
    AuthType,
    OAuthSpec,
    ToolError,
    Toolkit,
    ToolResult,
    ToolSpec,
    time_budget,
)
from aisquare.pipe import revoke_token as oauth_revoke_token

from aisquare_pipe_googlecalendar.errors import failure
from aisquare_pipe_googlecalendar.rest import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_RETRY_WAIT,
    GoogleCalendarAPIError,
    GoogleCalendarRestClient,
)
from aisquare_pipe_googlecalendar.tools import (
    COVERING_SCOPES,
    READONLY_SCOPE,
    TOOL_SPECS,
    TOOLS,
    apply_aliases,
)

logger = logging.getLogger("aisquare.pipe.googlecalendar")

#: A Google OAuth 2.0 web client, consented read-only by default
#: (``calendar.readonly``: calendars, events, free/busy). Hosts that enable
#: creating, changing or deleting events request ``calendar.events`` alongside
#: it (each tool's ``ToolSpec.required_scopes``). Both are Google-*sensitive* scopes:
#: production apps need OAuth verification. ``access_type=offline`` +
#: ``prompt=consent`` make Google return a refresh token on every consent;
#: access tokens expire after an hour. Google does not rotate refresh tokens.
#: Unlike Google Drive's spec there is no ``include_granted_scopes``: one
#: OAuth client may serve Drive, Gmail and Calendar, and a Calendar token
#: must carry Calendar's scopes only.
GOOGLECALENDAR_OAUTH = OAuthSpec(
    provider="google",
    authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
    token_url="https://oauth2.googleapis.com/token",
    revoke_url="https://oauth2.googleapis.com/revoke",
    default_scopes=(READONLY_SCOPE,),
    extra_authorize_params={
        "access_type": "offline",
        "prompt": "consent",
    },
    pkce=True,
    token_expires=True,
)


class GoogleCalendarToolkit(Toolkit):
    """Google Calendar over the Calendar v3 REST API: calendars, events
    (recurring ones and their instances), free/busy and free slots; create,
    change, quick-add and delete events.

    ``config`` needs ``access_token`` — a Google OAuth access token from
    :data:`GOOGLECALENDAR_OAUTH` (it expires after an hour; hosts refresh it).
    Tool slugs and argument names match Composio's Google Calendar toolkit.
    Each tool's OAuth scopes are its ``ToolSpec.required_scopes`` (and in its
    description). The default consent covers the read tools only; with
    ``config["granted_scopes"]``, a tool whose scopes were not granted is
    neither listed nor run (:meth:`scope_granted` counts Google's full
    ``calendar`` scope).

    ``config["timeout_seconds"]`` (optional, set by hosts) bounds a whole
    call: request timeouts and rate-limit sleeps all fit inside it.

    Failures carry a canonical ``error_code`` (see :func:`errors.error_code`)
    and, for ``rate_limited``, ``retry_after``.

    Concurrency: the instance holds no mutable state and, by default, every
    call opens and closes its own HTTP session. ``session`` (tests, or a host
    that manages pooling) is the ``requests.Session`` every call goes through
    instead; ``max_retries`` / ``max_retry_wait`` bound rate-limit retries.
    """

    name = "googlecalendar"
    version = "0.1.0"
    display_name = "Google Calendar"
    description = (
        "Google Calendar: list calendars and events (recurring ones expanded), find events, "
        "check free/busy and find free slots across calendars and people; create, quick-add, "
        "update and delete events with guests, recurrence and Google Meet links."
    )
    auth_type = AuthType.OAUTH2
    oauth = GOOGLECALENDAR_OAUTH
    docs_url = "https://developers.google.com/calendar/api/v3/reference"
    category = "calendar"
    keywords = (
        "google calendar", "calendar", "gcal", "event", "meeting", "schedule", "agenda",
        "availability", "free time", "busy", "invite", "reschedule", "recurring", "appointment",
    )
    examples = (
        "what's on my calendar Friday",
        "when am I free tomorrow afternoon",
        "schedule a 30 minute call with Ann on Tuesday",
        "move my 3pm meeting to 4",
    )

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_retry_wait: float = DEFAULT_MAX_RETRY_WAIT,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._session = session
        self._max_retries = max_retries
        self._max_retry_wait = max_retry_wait
        self._sleep = sleep

    def tools(self) -> list[ToolSpec]:
        return list(TOOL_SPECS)

    def scope_granted(self, scope: str, granted: frozenset[str]) -> bool:
        """Whether ``granted`` covers ``scope``, counting Google's full
        ``calendar`` scope, which covers both scopes the tools list."""
        return scope in granted or not COVERING_SCOPES.get(scope, frozenset()).isdisjoint(granted)

    def call_tool(
        self, slug: str, arguments: Mapping[str, Any] | None, config: Mapping[str, Any] | None
    ) -> ToolResult:
        """Core's ``call_tool``, after mapping the other spelling of an
        argument (``calendar_id`` for ``calendarId`` and back, Composio's
        aliases) onto the schema's, so validation sees the names the schema
        declares."""
        tool = TOOLS.get(slug.upper()) if isinstance(slug, str) else None
        if tool is not None and isinstance(arguments, Mapping):
            try:
                arguments = apply_aliases(tool.spec, arguments, tool.aliases)
            except Exception as e:  # noqa: BLE001 — call_tool never raises; validation reports it
                logger.debug("googlecalendar: argument aliases skipped (%s)", type(e).__name__)
        return super().call_tool(slug, arguments, config)

    def run_tool(
        self, tool: ToolSpec, arguments: dict[str, Any], config: dict[str, Any]
    ) -> ToolResult:
        entry = TOOLS.get(tool.slug)
        if entry is None:
            raise ToolError(f"{tool.slug} is not implemented")
        with self.client(config) as client:
            try:
                return entry.handler(client, arguments)
            except GoogleCalendarAPIError as e:
                return failure(e, entry.scopes)

    def client(self, config: Mapping[str, Any]) -> GoogleCalendarRestClient:
        """A REST client bound to ``config["access_token"]``."""
        return GoogleCalendarRestClient(
            str(config["access_token"]).strip(),
            session=self._session,
            max_retries=self._max_retries,
            max_retry_wait=self._max_retry_wait,
            budget=time_budget(config),
            sleep=self._sleep,
        )

    def revoke_token(
        self,
        *,
        client_id: str,
        client_secret: str | None,
        token: str,
        token_type_hint: str = "access_token",
    ) -> bool:
        """Disconnect: revoke the token at ``oauth2.googleapis.com/revoke``.
        Google revokes the user's whole grant to the OAuth client: with one
        client shared by several Google toolkits, this also ends the user's
        other Google connections (see the README). Never raises."""
        if self.oauth is None:
            return False
        try:
            return oauth_revoke_token(
                self.oauth,
                client_id=client_id,
                client_secret=client_secret,
                token=token,
                token_type_hint=token_type_hint,
            )
        except Exception as e:  # noqa: BLE001 — best effort by contract
            logger.warning("googlecalendar: token revoke failed (%s)", type(e).__name__)
            return False
