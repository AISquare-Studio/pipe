"""SlackToolkit — Slack's LLM-callable tools, acting as the user or as the app's bot."""

from __future__ import annotations

import dataclasses
import math
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import requests
from aisquare.pipe import (
    ERROR_CONFLICT,
    ERROR_FORBIDDEN,
    ERROR_INVALID_ARGUMENTS,
    ERROR_NOT_FOUND,
    ERROR_RATE_LIMITED,
    ERROR_TIMEOUT,
    ERROR_UNAUTHORIZED,
    ERROR_UPSTREAM_ERROR,
    AuthType,
    OAuthError,
    OAuthToken,
    ToolError,
    Toolkit,
    ToolResult,
    ToolSpec,
    error_code_for_status,
    time_budget,
)
from aisquare.pipe.core.oauth import build_authorize_url as core_build_authorize_url

from aisquare_pipe_slack.config import (
    SlackConfigError,
    SlackSettings,
    settings_from_config,
)
from aisquare_pipe_slack.constants import (
    CONFIG_ENABLED_TIERS,
    CONFIG_ENABLED_TOOLS,
    CONFIG_IDENTITY,
    IDENTITY_BOT,
    IDENTITY_USER,
)
from aisquare_pipe_slack.oauth import (
    SLACK_OAUTH,
    SlackInstallation,
    parse_installation,
    revoke_user_token,
    token_request,
)
from aisquare_pipe_slack.rest import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_RETRY_WAIT,
    RATE_LIMIT_ERRORS,
    SlackAPIError,
    SlackWebClient,
)
from aisquare_pipe_slack.scopes import OAuthIdentity, scopes_for
from aisquare_pipe_slack.tools import TOOL_SPECS, TOOLS, SlackCall, SlackTool

# --------------------------------------------------------------------------
# Errors: Slack's ok:false codes -> ERROR_*
# --------------------------------------------------------------------------

_KINDS: dict[str, frozenset[str]] = {
    ERROR_UNAUTHORIZED: frozenset(
        {"not_authed", "invalid_auth", "account_inactive", "token_revoked", "token_expired"}
    ),
    ERROR_FORBIDDEN: frozenset(
        {
            "missing_scope", "no_permission", "not_allowed_token_type", "restricted_action",
            "not_authorized", "ekm_access_denied", "team_access_not_granted",
            "enterprise_is_restricted", "accesslimited", "two_factor_setup_required",
            "cant_update_message", "cant_delete_message", "not_in_channel",
            "plan_upgrade_required", "paid_only", "permission_denied", "cant_kick_self",
            "cant_kick_from_general", "cant_archive_general", "cant_leave_general",
            "restricted_action_read_only_channel", "user_is_restricted",
        }
    ),
    ERROR_NOT_FOUND: frozenset(
        {
            "channel_not_found", "user_not_found", "users_not_found", "message_not_found",
            "thread_not_found", "file_not_found", "file_comment_not_found", "no_item_specified",
            "canvas_not_found", "bookmark_not_found", "no_such_subteam", "team_not_found",
            "not_found", "invalid_scheduled_message_id", "no_pin", "bad_timestamp",
            "user_not_visible",
        }
    ),
    ERROR_CONFLICT: frozenset(
        {
            "already_reacted", "no_reaction", "already_pinned", "not_pinned", "name_taken",
            "is_archived", "already_archived", "not_archived", "already_in_channel",
            "channel_canvas_already_exists", "cant_invite_self", "edit_window_closed",
            "message_limit_exceeded", "too_many_reactions", "too_many_pins",
        }
    ),
    ERROR_RATE_LIMITED: frozenset({*RATE_LIMIT_ERRORS, "restricted_too_many"}),
    ERROR_INVALID_ARGUMENTS: frozenset(
        {
            "invalid_arguments", "invalid_arg_name", "invalid_array_arg", "invalid_cursor",
            "invalid_ts_latest", "invalid_ts_oldest", "msg_too_long", "msg_blocks_too_long",
            "no_text", "invalid_blocks", "invalid_blocks_format", "invalid_name",
            "invalid_name_required", "invalid_name_punctuation", "invalid_name_maxlength",
            "invalid_name_specials", "time_in_past", "time_too_far", "too_many_users",
            "user_not_in_channel", "invalid_user", "snippet_too_large", "invalid_charset",
            "invalid_form_data", "invalid_post_type", "missing_post_type", "markdown_text_conflict",
            "invalid_presence", "invalid_profile", "invalid_users", "invalid_email",
            "invalid_name_or_value", "invalid_metadata_format", "invalid_metadata_schema",
            "invalid_post_at", "invalid_time", "too_many_attachments", "no_user",
            "no_reaction_name", "invalid_name_emoji", "not_enough_users", "too_many_emoji",
            "too_long", "invalid_types", "invalid_limit", "invalid_auth_type",
        }
    ),
    # Slack's "request_timeout" means "POST data missing or truncated", not a
    # server timeout; it is a failed request like any other upstream error.
    ERROR_UPSTREAM_ERROR: frozenset(
        {"fatal_error", "internal_error", "service_unavailable", "request_timeout"}
    ),
}
_KIND_OF: dict[str, str] = {code: kind for kind, codes in _KINDS.items() for code in codes}

#: The tool that fixes ``not_in_channel``.
_JOIN_TOOL = "SLACK_JOIN_AN_EXISTING_CONVERSATION"


def error_code(error: SlackAPIError) -> str:
    """The canonical ``ToolResult.error_code`` for a Slack failure, in this
    order: transport (client timeout, then network failure), then the HTTP
    status (429 rate limit, 408/504 timeout, other 5xx upstream, other
    non-2xx through core's ``error_code_for_status``), then Slack's
    ``ok: false`` code. Unknown codes are ``upstream_error``."""
    if error.timed_out:
        return ERROR_TIMEOUT
    if error.status == 0:
        return ERROR_UPSTREAM_ERROR
    if error.status == 429 or error.retry_after is not None:
        return ERROR_RATE_LIMITED
    if not 200 <= error.status < 300:
        if error.status >= 500 and error.status != 504:
            return ERROR_UPSTREAM_ERROR
        return error_code_for_status(error.status)
    return _KIND_OF.get(error.error, ERROR_UPSTREAM_ERROR)


def describe_error(error: SlackAPIError, *, identity: str = IDENTITY_USER) -> str:
    """``"<method>: <slack error> — <what to do>"``. Never carries arguments
    or the token."""
    code = error_code(error)
    text = f"{error.method}: {error.error}"
    if error.timed_out:
        return f"{text} — Slack did not answer in time"
    if error.error == "network_error":
        return f"{text} — could not reach Slack"
    if code == ERROR_RATE_LIMITED:
        if error.retry_after is not None:
            return f"{text} — Slack rate limit; retry in about {math.ceil(error.retry_after)}s"
        return f"{text} — Slack rate limit; wait before retrying"
    if code == ERROR_UNAUTHORIZED:
        return f"{text} — the Slack connection is no longer valid; reconnect Slack"
    if error.error == "missing_scope":
        needed = f"`{error.needed}`" if error.needed else "the missing scope"
        return f"{text} — reconnect Slack to grant {needed}"
    if error.error == "not_in_channel":
        return f"{text} — join the channel first with {_JOIN_TOOL} (or invite the app)"
    if error.error == "not_allowed_token_type":
        other = IDENTITY_BOT if identity == IDENTITY_USER else IDENTITY_USER
        return (
            f"{text} — Slack does not accept a {identity} token here; this needs the {other} "
            f"identity (config[{CONFIG_IDENTITY!r}])"
        )
    if code == ERROR_FORBIDDEN:
        return f"{text} — this Slack connection is not allowed to do that"
    if code == ERROR_NOT_FOUND:
        return f"{text} — it does not exist, or this Slack connection can't see it"
    if code == ERROR_CONFLICT:
        return f"{text} — already in that state; no change needed"
    if error.error == "invalid_cursor":
        return f"{text} — the cursor is invalid or expired; start again without it"
    if error.error in ("time_in_past", "time_too_far", "invalid_post_at", "invalid_time"):
        return f"{text} — post_at must be in the future and at most 120 days ahead"
    if error.error in ("msg_too_long", "msg_blocks_too_long"):
        return f"{text} — the message is too long; shorten it"
    if code == ERROR_INVALID_ARGUMENTS:
        return f"{text} — check the arguments"
    if error.status >= 500 or error.error in _KINDS[ERROR_UPSTREAM_ERROR]:
        return f"{text} — Slack had a problem; try again later"
    return text


def failure(error: SlackAPIError, *, identity: str = IDENTITY_USER) -> ToolResult:
    """A failed ToolResult: message, canonical ``error_code``, ``retry_after``
    (whole seconds) on a rate limit, and ``data={"slack_error", "status"}``."""
    return ToolResult.fail(
        describe_error(error, identity=identity),
        data={"slack_error": error.error, "status": error.status},
        code=error_code(error),
        retry_after=float(math.ceil(error.retry_after)) if error.retry_after is not None else None,
    )


def gate(tool: SlackTool, settings: SlackSettings) -> ToolResult | None:
    """The forbidden result for a tool this connection may not run — a tier
    it has not enabled, or a user-only tool under the bot identity — else
    None."""
    slug = tool.spec.slug
    if not settings.allows(slug, tool.tier):
        return ToolResult.fail(
            f"{slug} is a {tool.tier!r} tool and is not enabled for this Slack connection: the "
            f"host enables it by adding {tool.tier!r} to config[{CONFIG_ENABLED_TIERS!r}] or "
            f"{slug!r} to config[{CONFIG_ENABLED_TOOLS!r}]",
            code=ERROR_FORBIDDEN,
        )
    if settings.identity not in tool.identities:
        return ToolResult.fail(
            f"{slug} needs the user identity — Slack only accepts user tokens for it; this "
            f"connection acts as the {settings.identity} (config[{CONFIG_IDENTITY!r}])",
            code=ERROR_FORBIDDEN,
        )
    return None


class SlackToolkit(Toolkit):
    """Slack messages, history and threads, search, channels and DMs,
    reactions, pins, people and presence, over the Web API.

    ``config``:

    * ``access_token`` (required) — the token for the chosen identity: a user
      token (``xoxp-…``) or the app's bot token (``xoxb-…``);
    * ``identity`` — ``"user"`` (default: act as the person who connected)
      or ``"bot"`` (post as the app's bot user);
    * ``enabled_tiers`` — tiers this connection may call (default
      ``["read", "send"]``); add ``"write"`` / ``"destructive"`` explicitly;
    * ``enabled_tools`` — extra tool slugs enabled one by one;
    * ``attribution_name`` — the ``<name>`` in the "Sent by <name>" line on
      messages (default ``"pipe"``); always shown for the user identity;
    * ``bot_attribution`` — also show it on bot messages (default false);
    * ``timeout_seconds`` (set by hosts) — bounds a whole call, retries
      included.

    A tool outside the enabled tiers fails with ``forbidden`` naming the
    config key that enables it; :meth:`tools_for` lists only the enabled
    ones. Failures carry a canonical ``error_code`` (see :func:`error_code`)
    and, for ``rate_limited``, ``retry_after``.

    Concurrency: the instance holds no mutable state and, by default, every
    call opens and closes its own HTTP session. ``session`` (tests, or a host
    that manages pooling) is the ``requests.Session`` every call goes
    through instead; ``max_retries`` / ``max_retry_wait`` bound retries.
    """

    name = "slack"
    version = "0.1.0"
    display_name = "Slack"
    description = (
        "Slack messages, channel history and threads, search, channels and DMs, reactions, "
        "pins, people and presence — as the connected user or as the app's bot."
    )
    auth_type = AuthType.OAUTH2
    oauth = SLACK_OAUTH
    docs_url = "https://docs.slack.dev/reference/methods"

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

    # -- catalog ---------------------------------------------------------------

    def tools(self) -> list[ToolSpec]:
        return list(TOOL_SPECS)

    def tools_for(self, config: Mapping[str, Any] | None) -> list[ToolSpec]:
        """The tools ``config`` lets this connection call (enabled tier or
        slug, and runnable under its identity) — what a host should list to
        a model. Empty for a config the toolkit cannot use."""
        try:
            settings = settings_from_config(config if isinstance(config, Mapping) else {})
        except SlackConfigError:
            return []
        return [TOOLS[spec.slug].spec for spec in TOOL_SPECS if gate(TOOLS[spec.slug], settings) is None]

    def available_tools(self, config: Mapping[str, Any] | None = None) -> list[ToolSpec]:
        """Core's listing hook (``aisquare-pipe`` with ``available_tools``;
        ``search_tools(..., config=...)`` ranks it): :meth:`tools_for` for a
        connection's ``config``. Without a config — no connection to judge —
        the whole catalog, as :meth:`tools`. On an older core this is just an
        extra method; ``call_tool`` refuses disabled tools either way."""
        if config is None:
            return self.tools()
        return self.tools_for(config)

    @staticmethod
    def tier_of(slug: str) -> str | None:
        """A tool's risk tier (read, send, write, destructive), or None."""
        tool = TOOLS.get(slug.upper()) if isinstance(slug, str) else None
        return tool.tier if tool else None

    # -- execution ---------------------------------------------------------------

    def call_tool(
        self, slug: str, arguments: Mapping[str, Any] | None, config: Mapping[str, Any] | None
    ) -> ToolResult:
        """Core's ``call_tool``, with the tier and identity gate checked
        first: a tool the connection may not run fails as ``forbidden``
        before its arguments are looked at."""
        blocked: ToolResult | None = None
        try:
            spec = self.get_tool(slug)
            if spec is not None and isinstance(config, Mapping):
                blocked = gate(TOOLS[spec.slug], settings_from_config(config))
        except Exception:  # noqa: BLE001 — never raises; run_tool repeats the check
            blocked = None
        if blocked is not None:
            return blocked
        return super().call_tool(slug, arguments, config)

    def run_tool(
        self, tool: ToolSpec, arguments: dict[str, Any], config: dict[str, Any]
    ) -> ToolResult:
        row = TOOLS.get(tool.slug)
        if row is None:
            raise ToolError(f"{tool.slug} is not implemented")
        try:
            settings = settings_from_config(config)
        except SlackConfigError as e:
            raise ToolError(f"invalid Slack toolkit config: {e}", code=ERROR_UPSTREAM_ERROR) from None
        blocked = gate(row, settings)
        if blocked is not None:
            return blocked
        with self.client(config) as client:
            try:
                return row.handler(SlackCall(client, settings, idempotent=row.spec.read_only), arguments)
            except SlackAPIError as e:
                return failure(e, identity=settings.identity)

    def client(self, config: Mapping[str, Any]) -> SlackWebClient:
        """A Web API client bound to ``config["access_token"]``."""
        return SlackWebClient(
            str(config["access_token"]).strip(),
            session=self._session,
            max_retries=self._max_retries,
            max_retry_wait=self._max_retry_wait,
            budget=time_budget(config),
            sleep=self._sleep,
        )

    # -- connection (OAuth) --------------------------------------------------------

    def build_authorize_url(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        state: str,
        scopes: Sequence[str] | None = None,
        code_challenge: str | None = None,
        identity: OAuthIdentity = "user",
    ) -> str:
        """Slack's consent URL. ``identity`` picks the token(s) the consent
        mints: ``"user"`` (default), ``"bot"``, or ``"both"`` in one consent.
        ``scopes`` mixes scope-group names (see
        :data:`~aisquare_pipe_slack.scopes.SCOPE_GROUPS`) and raw Slack
        scopes; None asks for every scope the catalog uses. User scopes go
        in ``user_scope``, bot scopes in ``scope``."""
        user_scopes, bot_scopes = scopes_for(scopes, identity)
        spec = dataclasses.replace(
            SLACK_OAUTH,
            extra_authorize_params={"user_scope": ",".join(user_scopes)} if user_scopes else {},
        )
        return core_build_authorize_url(
            spec,
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            scopes=list(bot_scopes),
            code_challenge=code_challenge,
        )

    def exchange_installation(
        self,
        *,
        client_id: str,
        client_secret: str | None,
        code: str,
        redirect_uri: str,
    ) -> SlackInstallation:
        """Trade the code for everything the consent minted: the user token,
        the bot token, and the team / user / bot-user IDs. For a host that
        stores both tokens and switches identity by config."""
        payload = token_request(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
            client_id=client_id,
            client_secret=client_secret,
            step="exchange",
            session=self._session,
        )
        return parse_installation(payload)

    def exchange_code(
        self,
        *,
        client_id: str,
        client_secret: str | None,
        code: str,
        redirect_uri: str,
        code_verifier: str | None = None,
        identity: str | None = None,
    ) -> OAuthToken:
        """Core's one-token exchange: the token for ``identity`` (``"user"``
        or ``"bot"``); by default the user token when the consent minted one,
        else the bot token. ``code_verifier`` is unused (PKCE is off)."""
        installation = self.exchange_installation(
            client_id=client_id, client_secret=client_secret, code=code, redirect_uri=redirect_uri
        )
        return _pick(installation, identity)

    def refresh_access_token(
        self,
        *,
        client_id: str,
        client_secret: str | None,
        refresh_token: str,
        scopes: Sequence[str] | None = None,
        previous_scope: str | None = None,
    ) -> OAuthToken:
        """Refresh a rotating token (only when token rotation is on for the
        app). Keeps ``refresh_token`` when Slack returns none; a missing
        scope falls back to ``previous_scope``. ``invalid_refresh_token``
        comes back as ``invalid_grant``. Slack takes no scope on refresh."""
        payload = token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token},
            client_id=client_id,
            client_secret=client_secret,
            step="refresh",
            session=self._session,
        )
        token = _pick(parse_installation(payload), None)
        if token.refresh_token is None:
            token.refresh_token = refresh_token
        if not token.scope:
            token.scope = previous_scope or ""
        return token

    def revoke_token(
        self,
        *,
        client_id: str,
        client_secret: str | None,
        token: str,
        token_type_hint: str = "access_token",
    ) -> bool:
        """Disconnect: revoke this user token (``auth.revoke``). A bot token
        is never revoked — it is shared by every connection in the workspace
        — and returns False. Whether to call this at all is the host's
        decision: if Slack hands the same user token to two connections of
        one person, revoking it from one breaks the other. Never raises."""
        return revoke_user_token(token, session=self._session)


def _pick(installation: SlackInstallation, identity: str | None) -> OAuthToken:
    if identity not in (None, IDENTITY_USER, IDENTITY_BOT):
        raise OAuthError(
            f"slack: identity must be 'user' or 'bot', got {identity!r}",
            provider="slack",
            error="invalid_request",
        )
    if identity == IDENTITY_BOT:
        token = installation.bot_token
    elif identity == IDENTITY_USER:
        token = installation.user_token
    else:
        token = installation.user_token or installation.bot_token
    if token is None:
        raise OAuthError(
            f"slack: the consent minted no {identity} token (were {identity} scopes requested?)",
            provider="slack",
            error="invalid_token_response",
        )
    return token


__all__ = [
    "SlackToolkit",
    "describe_error",
    "error_code",
    "failure",
    "gate",
]
