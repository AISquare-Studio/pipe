"""GitHubToolkit — GitHub's LLM-callable tools plus its OAuth App connection spec."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any

import requests
from aisquare.pipe import AuthType, OAuthSpec, ToolError, Toolkit, ToolResult, ToolSpec

from aisquare_pipe_github.rest import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_RETRY_WAIT,
    GitHubAPIError,
    GitHubRestClient,
    revoke_grant,
)
from aisquare_pipe_github.tools import HANDLERS, TOOL_SPECS, normalize_arguments

#: A GitHub OAuth App. Its tokens do not expire and come without a refresh
#: token; GitHub answers the token endpoint form-encoded unless asked for
#: JSON, which core's flow handles. Revocation is not RFC 7009 — see
#: :meth:`GitHubToolkit.revoke_token`.
GITHUB_OAUTH = OAuthSpec(
    provider="github",
    authorize_url="https://github.com/login/oauth/authorize",
    token_url="https://github.com/login/oauth/access_token",
    default_scopes=("repo", "read:user", "read:org"),
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
    if error.status == 404:
        return f"{text} — it does not exist, or this GitHub connection cannot access it"
    return text


def failure(error: GitHubAPIError) -> ToolResult:
    """A failed ToolResult whose ``data`` lets callers branch without parsing
    the message: ``status``, plus ``reconnect_required`` on a 401 and
    ``retry_after`` (seconds) on a rate limit."""
    data: dict[str, Any] = {"status": error.status}
    if error.status == 401:
        data["reconnect_required"] = True
    if error.retry_after is not None:
        data["retry_after"] = math.ceil(error.retry_after)
    return ToolResult.fail(describe_error(error), data=data)


class GitHubToolkit(Toolkit):
    """GitHub repositories, code, issues, pull requests, releases, Actions
    runs and notifications over the REST API.

    ``config`` needs ``access_token`` — an OAuth App user token from
    :data:`GITHUB_OAUTH` (a personal access token works too). Tool slugs and
    argument names match Composio's GitHub toolkit.

    Constructor arguments are for tests and hosts: ``session`` is the
    ``requests.Session`` every call goes through (default: a fresh one per
    call); ``max_retries`` / ``max_retry_wait`` bound rate-limit retries.
    """

    name = "github"
    version = "0.2.0"
    display_name = "GitHub"
    description = (
        "GitHub repositories, code search and file contents, issues, pull requests and "
        "reviews, releases, Actions runs and notifications."
    )
    auth_type = AuthType.OAUTH2
    oauth = GITHUB_OAUTH
    docs_url = "https://docs.github.com/en/rest"

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
        """Disconnect: revoke the user's grant of this OAuth App
        (``DELETE /applications/{client_id}/grant``), which invalidates every
        token the app holds for them. GitHub has no RFC 7009 endpoint, and
        ``token_type_hint`` does not apply. Never raises."""
        if not client_secret:
            return False
        return revoke_grant(
            client_id=client_id,
            client_secret=client_secret,
            access_token=token,
            session=self._session,
        )
