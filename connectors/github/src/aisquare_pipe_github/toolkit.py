"""GitHubToolkit — GitHub's LLM-callable tools plus its OAuth App connection spec."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
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

from aisquare_pipe_github.rest import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_RETRY_WAIT,
    GitHubAPIError,
    GitHubRestClient,
    revoke_access_token,
    revoke_grant,
)
from aisquare_pipe_github.tools import (
    COVERING_SCOPES,
    HANDLERS,
    TOOL_SPECS,
    normalize_arguments,
)

#: A GitHub OAuth App. Its tokens do not expire and come without a refresh
#: token; GitHub answers the token endpoint form-encoded unless asked for
#: JSON, which core's flow handles. Revocation is per token, not RFC 7009 — see
#: :meth:`GitHubToolkit.revoke_token`.
#:
#: PKCE (S256 only; GitHub rejects ``plain``) is on: GitHub's web flow takes
#: ``code_challenge`` / ``code_challenge_method`` and checks ``code_verifier``
#: at the token endpoint, which binds each code to the flow that requested it
#: so a code cannot be redeemed under another pending ``state``. Core then
#: requires a challenge to build the authorize URL and the verifier to
#: exchange. The client secret is still sent: GitHub requires it for OAuth Apps.
GITHUB_OAUTH = OAuthSpec(
    provider="github",
    authorize_url="https://github.com/login/oauth/authorize",
    token_url="https://github.com/login/oauth/access_token",
    default_scopes=("repo", "read:user", "read:org"),
    pkce=True,
    token_expires=False,
)


def describe_error(error: GitHubAPIError) -> str:
    """``"<status>: <GitHub's message>"`` plus what the caller should do."""
    if not error.status:
        return error.message
    text = f"{error.status}: {error.message}"
    if error.status == 401:
        return (
            f"{text} — GitHub rejected this connection's access token (revoked or "
            "expired); the GitHub connection must be reconnected"
        )
    if error.retry_after is not None:
        return f"{text} — GitHub rate limit; retry in about {math.ceil(error.retry_after)}s"
    if error.status == 403:
        return (
            f"{text} — this GitHub connection is not allowed to do that (missing scope or "
            "permission, or the organization has not approved the app)"
        )
    if error.status == 404:
        return f"{text} — it does not exist, or this GitHub connection cannot access it"
    return text


def error_code(error: GitHubAPIError) -> str:
    """The canonical ``ToolResult.error_code`` for a GitHub failure: rate
    limits (429, or a rate-limit 403) are ``rate_limited`` and client-side
    timeouts ``timeout``; HTTP statuses map through core's
    ``error_code_for_status`` (401 unauthorized, 403 forbidden, 404 not_found,
    422 invalid_arguments, 409 conflict, ...); network failures and 5xx are
    ``upstream_error``."""
    if error.retry_after is not None:
        return ERROR_RATE_LIMITED
    if error.timed_out:
        return ERROR_TIMEOUT
    if not error.status:
        return ERROR_UPSTREAM_ERROR
    return error_code_for_status(error.status)


def failure(error: GitHubAPIError) -> ToolResult:
    """A failed ToolResult: message, canonical ``error_code``, ``retry_after``
    (whole seconds) on a rate limit, and ``data={"status": <HTTP status>}``
    when GitHub answered."""
    return ToolResult.fail(
        describe_error(error),
        data={"status": error.status} if error.status else None,
        code=error_code(error),
        retry_after=float(math.ceil(error.retry_after)) if error.retry_after is not None else None,
    )


class GitHubToolkit(Toolkit):
    """GitHub repositories, code, issues, pull requests, releases, Actions
    runs and notifications over the REST API.

    ``config`` needs ``access_token`` — an OAuth App user token from
    :data:`GITHUB_OAUTH` (a personal access token works too). Tool slugs and
    argument names match Composio's GitHub toolkit.

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

    name = "github"
    version = "0.3.0"
    display_name = "GitHub"
    description = (
        "GitHub repositories, code search and file contents, issues, pull requests and "
        "reviews, releases, Actions runs and notifications."
    )
    auth_type = AuthType.OAUTH2
    oauth = GITHUB_OAUTH
    docs_url = "https://docs.github.com/en/rest"
    category = "code"
    keywords = (
        "github", "repo", "repository", "code", "pull request", "PR", "review", "issue", "bug",
        "commit", "branch", "release", "actions", "CI", "workflow run", "notifications",
    )
    examples = (
        "my open PRs",
        "issues assigned to me in the api repo",
        "what changed in the last commit on main",
        "open an issue about the login bug",
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

    def run_tool(
        self, tool: ToolSpec, arguments: dict[str, Any], config: dict[str, Any]
    ) -> ToolResult:
        handler = HANDLERS.get(tool.slug)
        if handler is None:
            raise ToolError(f"{tool.slug} is not implemented")
        with self.client(config) as client:
            try:
                return handler(client, normalize_arguments(arguments))
            except GitHubAPIError as e:
                return failure(e)

    def client(self, config: dict[str, Any]) -> GitHubRestClient:
        """A REST client bound to ``config["access_token"]``."""
        return GitHubRestClient(
            str(config["access_token"]).strip(),
            session=self._session,
            max_retries=self._max_retries,
            max_retry_wait=self._max_retry_wait,
            budget=time_budget(config),
            sleep=self._sleep,
        )

    def scope_granted(self, scope: str, granted: frozenset[str]) -> bool:
        """Whether ``granted`` covers ``scope``, counting GitHub's scope
        hierarchy: ``repo`` covers ``public_repo`` and ``notifications``,
        ``user`` covers ``read:user``, ``admin:org`` / ``write:org`` cover
        ``read:org``."""
        return scope in granted or not COVERING_SCOPES.get(scope, frozenset()).isdisjoint(granted)

    def revoke_grant(self, *, client_id: str, client_secret: str | None, token: str) -> bool:
        """Remove the app from the user's GitHub account
        (``DELETE /applications/{client_id}/grant``): every token that GitHub
        user granted this app dies — the user's other connections to it as
        well — and the app leaves their authorized OAuth apps. For "remove
        AISquare from my GitHub" or account deletion; a plain disconnect is
        :meth:`revoke_token`. ``token`` is any live token of the grant. False
        without a client secret (nothing is sent). Never raises."""
        if not client_secret:
            return False
        return revoke_grant(
            client_id=client_id,
            client_secret=client_secret,
            access_token=token,
            session=self._session,
        )

    def revoke_token(
        self,
        *,
        client_id: str,
        client_secret: str | None,
        token: str,
        token_type_hint: str = "access_token",
    ) -> bool:
        """Disconnect: revoke this connection's access token
        (``DELETE /applications/{client_id}/token``). Only this token dies;
        revoking the whole grant would also break every other connection the
        same GitHub user made to this app (that is :meth:`revoke_grant`).
        GitHub has no RFC 7009 endpoint, and ``token_type_hint`` does not
        apply. Never raises."""
        if not client_secret:
            return False
        return revoke_access_token(
            client_id=client_id,
            client_secret=client_secret,
            access_token=token,
            session=self._session,
        )
