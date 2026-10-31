"""GmailToolkit — Gmail's LLM-callable tools plus its Google OAuth spec."""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

import requests
from aisquare.pipe import (
    ERROR_INSUFFICIENT_SCOPE,
    ERROR_RATE_LIMITED,
    ERROR_TIMEOUT,
    ERROR_UPSTREAM_ERROR,
    AuthType,
    OAuthSpec,
    ToolError,
    Toolkit,
    ToolResult,
    ToolSpec,
    error_code_for_status,
    time_budget,
)
from aisquare.pipe import revoke_token as oauth_revoke_token

from aisquare_pipe_gmail.rest import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_RETRY_WAIT,
    GmailAPIError,
    GmailRestClient,
)
from aisquare_pipe_gmail.tools import (
    COVERING_SCOPES,
    READONLY_SCOPE,
    SCOPE_BASE,
    TOOL_SPECS,
    TOOLS,
    apply_aliases,
)

logger = logging.getLogger("aisquare.pipe.gmail")

#: A Google OAuth 2.0 web client, consented for read-only mail by default
#: (``gmail.readonly``). Hosts that enable drafts, sending or labels request
#: the extra scope each tool lists (its ``ToolSpec.required_scopes``) alongside
#: it. Every Gmail scope except ``gmail.send`` is Google-*restricted*:
#: production apps need OAuth verification plus a security assessment.
#: ``access_type=offline`` + ``prompt=consent`` make Google return a refresh
#: token on every consent; access tokens expire after an hour. Google does
#: not rotate refresh tokens. Unlike Google Drive's spec there is no
#: ``include_granted_scopes``: one OAuth client may serve Drive, Gmail and
#: Calendar, and a Gmail token must carry Gmail's scopes only.
GMAIL_OAUTH = OAuthSpec(
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


def _short(scopes: Iterable[str]) -> str:
    return ", ".join(s.removeprefix(SCOPE_BASE) for s in scopes)


def describe_error(error: GmailAPIError, scopes: Iterable[str] = ()) -> str:
    """``"<status>: <Google's message>"`` plus what the caller should do."""
    if not error.status:
        return error.message
    text = f"{error.status}: {error.message}"
    if error.status == 401:
        return (
            f"{text} — Google rejected this connection's access token (expired or revoked); "
            "refresh it, or the Gmail connection must be reconnected"
        )
    if error.retry_after is not None:
        seconds = whole_seconds(error.retry_after)
        when = f"in about {seconds:.0f}s" if seconds is not None else "later"
        return f"{text} — Gmail rate limit; retry {when}"
    if error.insufficient_scope:
        needed = _short(scopes)
        return (
            f"{text} — this Gmail connection was not granted the access this tool needs"
            + (f" ({needed})" if needed else "")
            + "; reconnect Gmail and approve it"
        )
    if error.status == 403:
        return (
            f"{text} — this Gmail connection is not allowed to do that (a domain policy, a quota, "
            "or a mailbox it may not act for)"
        )
    if error.status == 404:
        return f"{text} — it does not exist, or this Gmail connection cannot see it"
    return text


def error_code(error: GmailAPIError) -> str:
    """The canonical ``ToolResult.error_code`` for a Gmail failure: rate
    limits (429, or a 403 with reason ``rateLimitExceeded`` /
    ``userRateLimitExceeded``) are ``rate_limited``, a 403 for a missing
    OAuth scope is ``insufficient_scope`` and client-side timeouts
    ``timeout``; HTTP statuses map through core's ``error_code_for_status``
    (401 unauthorized, any other 403 forbidden, 404 not_found, 400
    invalid_arguments, ...); network failures and 5xx are ``upstream_error``."""
    if error.retry_after is not None:
        return ERROR_RATE_LIMITED
    if error.timed_out:
        return ERROR_TIMEOUT
    if not error.status:
        return ERROR_UPSTREAM_ERROR
    if error.status == 403 and error.insufficient_scope:
        return ERROR_INSUFFICIENT_SCOPE
    return error_code_for_status(error.status)


def whole_seconds(value: float | None) -> float | None:
    """``value`` rounded up to whole seconds; None when absent or not finite."""
    if value is None or not math.isfinite(value):
        return None
    return float(math.ceil(value))


def failure(error: GmailAPIError, scopes: Iterable[str] = ()) -> ToolResult:
    """A failed ToolResult: message, canonical ``error_code``, ``retry_after``
    (whole seconds) on a rate limit, and ``data={"status", "reason"}`` when
    Google answered — plus ``required_scopes`` when a scope was missing."""
    data: dict[str, Any] | None = None
    if error.status:
        data = {"status": error.status}
        if error.reason:
            data["reason"] = error.reason
        if error.insufficient_scope:
            data["insufficient_scope"] = True
            data["required_scopes"] = list(scopes)
    return ToolResult.fail(
        describe_error(error, scopes),
        data=data,
        code=error_code(error),
        retry_after=whole_seconds(error.retry_after),
    )


class GmailToolkit(Toolkit):
    """Gmail over the Gmail v1 REST API: search and read mail, threads,
    attachments and labels; draft, send and reply; label, mark read and trash.

    ``config`` needs ``access_token`` — a Google OAuth access token from
    :data:`GMAIL_OAUTH` (it expires after an hour; hosts refresh it). Tool
    slugs and argument names match Composio's Gmail toolkit. Each tool's
    OAuth scopes are its ``ToolSpec.required_scopes`` (and in its
    description). The default consent covers the read tools only; with
    ``config["granted_scopes"]``, a tool whose scopes were not granted is
    neither listed nor run (:meth:`scope_granted` counts Google's broader
    scopes).

    ``config["timeout_seconds"]`` (optional, set by hosts) bounds a whole
    call: request timeouts and rate-limit sleeps all fit inside it.

    Failures carry a canonical ``error_code`` (see :func:`error_code`) and,
    for ``rate_limited``, ``retry_after``.

    Concurrency: the instance holds no mutable state and, by default, every
    call opens and closes its own HTTP session. ``session`` (tests, or a host
    that manages pooling) is the ``requests.Session`` every call goes through
    instead; ``max_retries`` / ``max_retry_wait`` bound rate-limit retries.
    """

    name = "gmail"
    version = "0.1.0"
    display_name = "Gmail"
    description = (
        "Gmail: search and read email, conversations and attachments, labels and the account "
        "profile; write drafts, send email and reply in threads; label, mark read/unread, "
        "archive and trash messages."
    )
    auth_type = AuthType.OAUTH2
    oauth = GMAIL_OAUTH
    docs_url = "https://developers.google.com/gmail/api/reference/rest"

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
        """Whether ``granted`` covers ``scope``, counting Google's broader
        Gmail scopes: ``https://mail.google.com/`` covers every one,
        ``gmail.modify`` covers read, compose and send, ``gmail.compose``
        covers send (each Gmail method's documented scope list)."""
        return scope in granted or not COVERING_SCOPES.get(scope, frozenset()).isdisjoint(granted)

    def call_tool(
        self, slug: str, arguments: Mapping[str, Any] | None, config: Mapping[str, Any] | None
    ) -> ToolResult:
        """Core's ``call_tool``, after mapping the other spelling of an
        argument (``messageId`` for ``message_id``, Composio's aliases) onto
        the schema's, so validation sees the names the schema declares."""
        tool = TOOLS.get(slug.upper()) if isinstance(slug, str) else None
        if tool is not None and isinstance(arguments, Mapping):
            try:
                arguments = apply_aliases(tool.spec, arguments, tool.aliases)
            except Exception as e:  # noqa: BLE001 — call_tool never raises; validation reports it
                logger.debug("gmail: argument aliases skipped (%s)", type(e).__name__)
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
            except GmailAPIError as e:
                return failure(e, entry.scopes)

    def client(self, config: Mapping[str, Any]) -> GmailRestClient:
        """A REST client bound to ``config["access_token"]``."""
        return GmailRestClient(
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
            logger.warning("gmail: token revoke failed (%s)", type(e).__name__)
            return False
