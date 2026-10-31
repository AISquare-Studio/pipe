"""GoogleDriveToolkit — Google Drive's LLM-callable tools plus its Google OAuth spec."""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Mapping
from typing import Any

import requests
from aisquare.pipe import (
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

from aisquare_pipe_googledrive.rest import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_RETRY_WAIT,
    GoogleDriveAPIError,
    GoogleDriveRestClient,
)
from aisquare_pipe_googledrive.tools import (
    COVERING_SCOPES,
    DRIVE_SCOPE,
    TOOL_SPECS,
    TOOLS,
    apply_aliases,
    normalize_arguments,
)

logger = logging.getLogger("aisquare.pipe.googledrive")

#: A Google OAuth 2.0 web client. ``access_type=offline`` + ``prompt=consent``
#: make Google return a refresh token on every consent (not only the first);
#: access tokens expire after an hour. Google does not rotate refresh tokens.
GOOGLEDRIVE_OAUTH = OAuthSpec(
    provider="google",
    authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
    token_url="https://oauth2.googleapis.com/token",
    revoke_url="https://oauth2.googleapis.com/revoke",
    default_scopes=(DRIVE_SCOPE,),
    extra_authorize_params={
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    },
    pkce=True,
    token_expires=True,
)


def describe_error(error: GoogleDriveAPIError) -> str:
    """``"<status>: <Google's message>"`` plus what the caller should do."""
    if not error.status:
        return error.message
    text = f"{error.status}: {error.message}"
    if error.status == 401:
        return (
            f"{text} — Google rejected this connection's access token (expired or revoked); "
            "refresh it, or the Google Drive connection must be reconnected"
        )
    if error.retry_after is not None:
        seconds = whole_seconds(error.retry_after)
        when = f"in about {seconds:.0f}s" if seconds is not None else "later"
        return f"{text} — Google Drive rate limit; retry {when}"
    if error.status == 403:
        return (
            f"{text} — this Google Drive connection is not allowed to do that (missing scope, "
            "no permission on the file, a quota, or a domain policy)"
        )
    if error.status == 404:
        return f"{text} — it does not exist, or this Google Drive connection cannot see it"
    return text


def error_code(error: GoogleDriveAPIError) -> str:
    """The canonical ``ToolResult.error_code`` for a Drive failure: rate
    limits (429, or a 403 with reason ``rateLimitExceeded`` /
    ``userRateLimitExceeded``) are ``rate_limited`` and client-side timeouts
    ``timeout``; HTTP statuses map through core's ``error_code_for_status``
    (401 unauthorized, any other 403 forbidden, 404 not_found, 400
    invalid_arguments, 409/412 conflict, ...); network failures and 5xx are
    ``upstream_error``."""
    if error.retry_after is not None:
        return ERROR_RATE_LIMITED
    if error.timed_out:
        return ERROR_TIMEOUT
    if not error.status:
        return ERROR_UPSTREAM_ERROR
    return error_code_for_status(error.status)


def whole_seconds(value: float | None) -> float | None:
    """``value`` rounded up to whole seconds; None when absent or not finite
    (``math.ceil`` of inf raises)."""
    if value is None or not math.isfinite(value):
        return None
    return float(math.ceil(value))


def failure(error: GoogleDriveAPIError) -> ToolResult:
    """A failed ToolResult: message, canonical ``error_code``, ``retry_after``
    (whole seconds) on a rate limit, and ``data={"status", "reason"}`` when
    Google answered."""
    data: dict[str, Any] | None = None
    if error.status:
        data = {"status": error.status}
        if error.reason:
            data["reason"] = error.reason
    return ToolResult.fail(
        describe_error(error),
        data=data,
        code=error_code(error),
        retry_after=whole_seconds(error.retry_after),
    )


class GoogleDriveToolkit(Toolkit):
    """Google Drive files, folders, exports of Docs/Sheets/Slides, sharing and
    comments over the Drive v3 REST API — My Drive and shared drives.

    ``config`` needs ``access_token`` — a Google OAuth access token from
    :data:`GOOGLEDRIVE_OAUTH` (it expires after an hour; hosts refresh it).
    Tool slugs and argument names match Composio's Google Drive toolkit.

    ``config["timeout_seconds"]`` (optional, set by hosts) bounds a whole
    call: request timeouts and rate-limit sleeps all fit inside it.

    Failures carry a canonical ``error_code`` (see :func:`error_code`) and,
    for ``rate_limited``, ``retry_after``.

    Concurrency: the instance holds no mutable state and, by default, every
    call opens and closes its own HTTP session, so concurrent calls on one
    instance or on fresh instances share nothing. ``session`` (tests, or a
    host that manages pooling) is the ``requests.Session`` every call goes
    through instead; ``max_retries`` / ``max_retry_wait`` bound rate-limit
    retries.
    """

    name = "googledrive"
    version = "0.3.0"
    display_name = "Google Drive"
    description = (
        "Google Drive: search, read and download files (Google Docs, Sheets and Slides "
        "exported), upload and create files, folders, move/copy/rename/trash, sharing "
        "permissions, comments and shared drives."
    )
    auth_type = AuthType.OAUTH2
    oauth = GOOGLEDRIVE_OAUTH
    docs_url = "https://developers.google.com/drive/api/reference/rest/v3"
    category = "files"
    keywords = (
        "google drive", "drive", "gdrive", "file", "folder", "google docs", "doc", "sheets",
        "spreadsheet", "slides", "deck", "pdf", "upload", "share", "shared drive",
    )
    examples = (
        "the deck Sam shared",
        "find the Q3 budget spreadsheet",
        "upload this report to the Marketing folder",
        "who has access to the roadmap doc",
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
        """Whether ``granted`` covers ``scope``, counting Google's Drive
        hierarchy: ``drive`` covers ``drive.readonly`` (and the narrower
        metadata and per-file scopes)."""
        return scope in granted or not COVERING_SCOPES.get(scope, frozenset()).isdisjoint(granted)

    def call_tool(
        self, slug: str, arguments: Mapping[str, Any] | None, config: Mapping[str, Any] | None
    ) -> ToolResult:
        """Core's ``call_tool``, after mapping the other spelling of an
        argument (``file_id`` for ``fileId`` and back, Composio's aliases)
        onto the schema's, so validation sees the names the schema declares."""
        tool = TOOLS.get(slug.upper()) if isinstance(slug, str) else None
        if tool is not None and isinstance(arguments, Mapping):
            try:
                arguments = apply_aliases(tool.spec, arguments, tool.aliases)
            except Exception as e:  # noqa: BLE001 — call_tool never raises; validation reports it
                logger.debug("googledrive: argument aliases skipped (%s)", type(e).__name__)
        return super().call_tool(slug, arguments, config)

    def run_tool(
        self, tool: ToolSpec, arguments: dict[str, Any], config: dict[str, Any]
    ) -> ToolResult:
        entry = TOOLS.get(tool.slug)
        if entry is None:
            raise ToolError(f"{tool.slug} is not implemented")
        with self.client(config) as client:
            try:
                return entry.handler(client, normalize_arguments(arguments))
            except GoogleDriveAPIError as e:
                return failure(e)

    def client(self, config: Mapping[str, Any]) -> GoogleDriveRestClient:
        """A REST client bound to ``config["access_token"]``."""
        return GoogleDriveRestClient(
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
        Revoking either token of a grant (access or refresh) revokes the whole
        grant. Never raises."""
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
            logger.warning("googledrive: token revoke failed (%s)", type(e).__name__)
            return False
