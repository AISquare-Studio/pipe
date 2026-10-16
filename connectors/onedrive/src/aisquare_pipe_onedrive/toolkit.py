"""OneDriveToolkit — OneDrive's LLM-callable tools plus its Microsoft identity
platform OAuth spec."""

from __future__ import annotations

import dataclasses
import math
import re
import time
from collections.abc import Callable, Sequence
from typing import Any

import requests
from aisquare.pipe import (
    ERROR_CONFLICT,
    ERROR_RATE_LIMITED,
    ERROR_TIMEOUT,
    ERROR_UPSTREAM_ERROR,
    AuthType,
    OAuthSpec,
    OAuthToken,
    ToolError,
    Toolkit,
    ToolResult,
    ToolSpec,
    build_authorize_url,
    error_code_for_status,
    exchange_code,
    refresh_access_token,
)

from aisquare_pipe_onedrive.rest import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_RETRY_WAIT,
    MAX_RETRY_AFTER,
    OneDriveAPIError,
    OneDriveRestClient,
)
from aisquare_pipe_onedrive.tools import HANDLERS, TOOL_SPECS

#: Placeholder for the Entra ID tenant in :data:`MICROSOFT_OAUTH`'s URLs.
TENANT_PLACEHOLDER = "{tenant}"
#: Personal Microsoft accounts and work/school accounts from any tenant.
DEFAULT_TENANT = "common"
# common / organizations / consumers, a tenant GUID, or a verified domain.
_TENANT = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")

#: A Microsoft identity platform (v2.0) app registration, delegated
#: permissions. The URLs carry a ``{tenant}`` placeholder that hosts fill per
#: app (``common``, ``organizations``, ``consumers`` or a tenant ID) — see
#: :func:`microsoft_oauth`; the toolkit fills ``common`` when nobody did.
#: Access tokens expire in about an hour and every refresh rotates the
#: refresh token. There is no revocation endpoint: see
#: :meth:`OneDriveToolkit.revoke_token`.
MICROSOFT_OAUTH = OAuthSpec(
    provider="microsoft",
    authorize_url=f"https://login.microsoftonline.com/{TENANT_PLACEHOLDER}/oauth2/v2.0/authorize",
    token_url=f"https://login.microsoftonline.com/{TENANT_PLACEHOLDER}/oauth2/v2.0/token",
    default_scopes=("offline_access", "User.Read", "Files.ReadWrite.All"),
    pkce=True,
    token_expires=True,
    rotates_refresh_token=True,
)


def microsoft_oauth(tenant: str | None = None, spec: OAuthSpec = MICROSOFT_OAUTH) -> OAuthSpec:
    """``spec`` with its ``{tenant}`` placeholder filled (default ``common``).
    A spec without the placeholder comes back unchanged. Raises ValueError
    for a tenant that is not a name, GUID or domain."""
    value = (tenant or DEFAULT_TENANT).strip()
    if not _TENANT.fullmatch(value):
        raise ValueError("tenant must be common, organizations, consumers, a tenant ID or a domain")
    urls: dict[str, Any] = {
        name: url.replace(TENANT_PLACEHOLDER, value)
        for name in ("authorize_url", "token_url", "revoke_url")
        if isinstance(url := getattr(spec, name), str) and TENANT_PLACEHOLDER in url
    }
    return dataclasses.replace(spec, **urls) if urls else spec


def retry_seconds(seconds: float) -> float:
    """Whole seconds to tell a caller to wait: rounded up and capped at
    ``MAX_RETRY_AFTER`` (a non-finite value is the cap). Never raises."""
    if not math.isfinite(seconds):
        return float(MAX_RETRY_AFTER)
    return float(math.ceil(min(max(seconds, 0.0), float(MAX_RETRY_AFTER))))


def describe_error(error: OneDriveAPIError) -> str:
    """``"<status>: <Graph code>: <message>"`` plus what the caller should do."""
    if not error.status:
        return error.message
    text = f"{error.status}: {error.message}"
    if error.status == 401:
        return (
            f"{text} — Microsoft rejected this connection's access token (expired or "
            "revoked); the OneDrive connection must be reconnected"
        )
    if error.retry_after is not None:
        return f"{text} — Microsoft Graph is throttling; retry in about {retry_seconds(error.retry_after):g}s"
    if error.status == 403:
        return (
            f"{text} — this OneDrive connection is not allowed to do that (missing consent "
            "for Files.ReadWrite.All, or no access to the item)"
        )
    if error.status == 404:
        return (
            f"{text} — it does not exist, or this OneDrive connection cannot access it "
            "(item IDs change after a move or delete; search again)"
        )
    if error.code == "nameAlreadyExists":
        return f"{text} — pick another name, or set conflict_behavior to 'rename' or 'replace'"
    if error.status == 412:
        return f"{text} — the item changed since that eTag was read; fetch it again"
    return text


def error_code(error: OneDriveAPIError) -> str:
    """The canonical ``ToolResult.error_code`` for a Graph failure: throttling
    (429, or 503/509 with Retry-After) is ``rate_limited`` and client-side
    timeouts ``timeout``; a locked item (423) is ``conflict``; other HTTP
    statuses map through core's ``error_code_for_status`` (401 unauthorized,
    403 forbidden, 404 not_found, 400 invalid_arguments, 409/412 conflict,
    ...); network failures and 5xx are ``upstream_error``."""
    if error.retry_after is not None:
        return ERROR_RATE_LIMITED
    if error.timed_out:
        return ERROR_TIMEOUT
    if not error.status:
        return ERROR_UPSTREAM_ERROR
    if error.status == 423:
        return ERROR_CONFLICT
    return error_code_for_status(error.status)


def failure(error: OneDriveAPIError) -> ToolResult:
    """A failed ToolResult: message, canonical ``error_code``, ``retry_after``
    (whole seconds) when throttled, and ``data={"status": <HTTP status>}``
    when Graph answered."""
    return ToolResult.fail(
        describe_error(error),
        data={"status": error.status} if error.status else None,
        code=error_code(error),
        retry_after=retry_seconds(error.retry_after) if error.retry_after is not None else None,
    )


def time_budget(config: dict[str, Any]) -> float | None:
    """The host's per-call budget, ``config["timeout_seconds"]`` (a positive
    number), or None."""
    value = config.get("timeout_seconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


class OneDriveToolkit(Toolkit):
    """OneDrive files and folders over Microsoft Graph: search, browse,
    download, upload, organize, and share.

    ``config`` needs ``access_token`` — a delegated (per-user) token from
    :data:`MICROSOFT_OAUTH`. App-only (client-credentials) tokens cannot call
    ``/me`` and do not work here. Tool slugs and argument names match
    Composio's ``one_drive`` toolkit.

    ``tenant`` fills the ``{tenant}`` placeholder in the OAuth URLs
    (``common`` by default; ``organizations`` for work/school accounts only,
    ``consumers`` for personal accounts only, or one tenant's ID or domain).
    A host may instead assign a filled spec to ``oauth`` on the instance.

    ``config["timeout_seconds"]`` (optional, set by hosts) bounds a whole
    call: request timeouts and throttling sleeps all fit inside it.

    Failures carry a canonical ``error_code`` (see :func:`error_code`) and,
    for ``rate_limited``, ``retry_after``.

    Concurrency: the instance holds no mutable state and, by default, every
    call opens and closes its own HTTP session, so concurrent calls on one
    instance or on fresh instances share nothing. ``session`` (tests, or a
    host that manages pooling) is the ``requests.Session`` every call goes
    through instead; ``max_retries`` / ``max_retry_wait`` bound throttling
    retries.
    """

    name = "onedrive"
    version = "0.2.0"
    display_name = "OneDrive"
    description = (
        "Microsoft OneDrive files and folders: search, browse, download, upload, create "
        "folders, move, copy, rename, delete, sharing links and permissions."
    )
    auth_type = AuthType.OAUTH2
    #: Composio's spelling (its toolkit is ``one_drive``), kept for compatibility.
    slug_prefix = "ONE_DRIVE_"
    oauth = MICROSOFT_OAUTH
    docs_url = "https://learn.microsoft.com/en-us/graph/api/resources/onedrive"

    def __init__(
        self,
        *,
        tenant: str | None = None,
        session: requests.Session | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_retry_wait: float = DEFAULT_MAX_RETRY_WAIT,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if tenant is not None:
            self.oauth = microsoft_oauth(tenant)
        self._session = session
        self._max_retries = max_retries
        self._max_retry_wait = max_retry_wait
        self._sleep = sleep

    def tools(self) -> list[ToolSpec]:
        return list(TOOL_SPECS)

    def run_tool(
        self, tool: ToolSpec, arguments: dict[str, Any], config: dict[str, Any]
    ) -> ToolResult:
        handler = HANDLERS.get(tool.slug)
        if handler is None:
            raise ToolError(f"{tool.slug} is not implemented")
        with self.client(config) as client:
            try:
                return handler(client, arguments)
            except OneDriveAPIError as e:
                return failure(e)

    def client(self, config: dict[str, Any]) -> OneDriveRestClient:
        """A Graph client bound to ``config["access_token"]``."""
        return OneDriveRestClient(
            str(config["access_token"]).strip(),
            session=self._session,
            max_retries=self._max_retries,
            max_retry_wait=self._max_retry_wait,
            budget=time_budget(config),
            sleep=self._sleep,
        )

    # -- connection (OAuth) ------------------------------------------------

    def _oauth_spec(self) -> OAuthSpec:
        """The spec with any unfilled ``{tenant}`` set to ``common``."""
        return microsoft_oauth(DEFAULT_TENANT, self._require_oauth())

    def build_authorize_url(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        state: str,
        scopes: Sequence[str] | None = None,
        code_challenge: str | None = None,
    ) -> str:
        return build_authorize_url(
            self._oauth_spec(),
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            scopes=scopes,
            code_challenge=code_challenge,
        )

    def exchange_code(
        self,
        *,
        client_id: str,
        client_secret: str | None,
        code: str,
        redirect_uri: str,
        code_verifier: str | None = None,
    ) -> OAuthToken:
        return exchange_code(
            self._oauth_spec(),
            client_id=client_id,
            client_secret=client_secret,
            code=code,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
        )

    def refresh_access_token(
        self,
        *,
        client_id: str,
        client_secret: str | None,
        refresh_token: str,
        scopes: Sequence[str] | None = None,
        previous_scope: str | None = None,
    ) -> OAuthToken:
        return refresh_access_token(
            self._oauth_spec(),
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
            scopes=scopes,
            previous_scope=previous_scope,
        )

    def revoke_token(
        self,
        *,
        client_id: str,
        client_secret: str | None,
        token: str,
        token_type_hint: str = "access_token",
    ) -> bool:
        """Always False, and nothing is sent: the Microsoft identity platform
        has no endpoint that revokes one app's delegated tokens. Disconnecting
        means the host forgets the stored token pair; the access token lapses
        within about an hour. (``revokeSignInSessions`` is deliberately not
        used — it signs the user out of every app on every device.) The user
        can remove the app's consent at https://myapps.microsoft.com or
        https://account.live.com/consent/Manage."""
        return False
