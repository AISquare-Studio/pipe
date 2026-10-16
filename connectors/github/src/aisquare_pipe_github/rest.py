"""GitHub REST API client for :class:`~aisquare_pipe_github.toolkit.GitHubToolkit`.

This module is the toolkit's only HTTP boundary (tests mock the
``requests.Session`` handed to it). One :class:`GitHubRestClient` serves one
tool call with one access token. The token rides the ``Authorization`` header
only: it never appears in a URL, a log line, an exception message or a repr.

Non-2xx answers raise :class:`GitHubAPIError` carrying the status and GitHub's
own message; the toolkit turns that into a failed ``ToolResult``.

Rate limits: a 429, or a 403 that GitHub marks as a rate limit, is retried up
to ``max_retries`` times, sleeping for what GitHub asks (``Retry-After``, else
until ``x-ratelimit-reset``). The total sleep per call is capped at
``max_retry_wait`` seconds; a longer wait fails at once (with
``GitHubAPIError.retry_after`` set) rather than holding a tool call for the
rest of an hourly window.

Time budget: with ``budget`` (seconds; the host's ``config["timeout_seconds"]``)
the whole call — every attempt and every retry sleep — fits inside it. Each
request's timeout shrinks to what is left, a retry that would sleep past it
fails at once, and running out raises a ``timed_out`` error.
"""

from __future__ import annotations

import logging
import time
import urllib.parse
from collections.abc import Callable, Mapping
from typing import Any, Self

import requests

logger = logging.getLogger("aisquare.pipe.github")

API_BASE = "https://api.github.com"
API_VERSION = "2022-11-28"
JSON_MEDIA = "application/vnd.github+json"
RAW_MEDIA = "application/vnd.github.raw+json"
USER_AGENT = "aisquare-pipe-github"

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_MAX_RETRY_WAIT = 60.0

#: GitHub's advice for a secondary rate limit that names no wait.
_SECONDARY_LIMIT_WAIT = 60.0
#: Backoff for a 502/503/504 on a GET (safe to repeat).
_SERVER_ERROR_WAIT = 1.0
_MAX_MESSAGE = 500


class GitHubAPIError(Exception):
    """A non-2xx GitHub answer (``status``) or a transport failure (``status=0``).

    ``message`` is GitHub's own error text (plus any validation details);
    ``retry_after`` is set when the failure is a rate limit, in seconds;
    ``timed_out`` when GitHub did not answer in time (or the budget ran out).
    """

    def __init__(
        self,
        status: int,
        message: str,
        *,
        retry_after: float | None = None,
        timed_out: bool = False,
    ) -> None:
        super().__init__(f"{status}: {message}" if status else message)
        self.status = status
        self.message = message
        self.retry_after = retry_after
        self.timed_out = timed_out

    @property
    def rate_limited(self) -> bool:
        return self.retry_after is not None


class GitHubRestClient:
    """Bearer-token client for ``api.github.com`` (one per tool call)."""

    def __init__(
        self,
        token: str,
        *,
        session: requests.Session | None = None,
        base_url: str = API_BASE,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_retry_wait: float = DEFAULT_MAX_RETRY_WAIT,
        budget: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._token = token
        self._owns_session = session is None
        self._session = session if session is not None else requests.Session()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.max_retry_wait = max(0.0, max_retry_wait)
        self._sleep = sleep
        self._clock = clock
        self._monotonic = monotonic
        self.budget = budget if budget is not None and budget > 0 else None
        self._deadline = monotonic() + self.budget if self.budget is not None else None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(base_url={self.base_url!r})"

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def _remaining(self) -> float | None:
        """Seconds left in the budget (None: unbounded)."""
        if self._deadline is None:
            return None
        return self._deadline - self._monotonic()

    # ------------------------------------------------------------------ http

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        accept: str = JSON_MEDIA,
    ) -> requests.Response:
        """Send one request (``path`` is relative to ``base_url``); return the
        2xx response or raise :class:`GitHubAPIError`."""
        headers = {
            "Accept": accept,
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }
        waited = 0.0
        attempt = 0
        while True:
            remaining = self._remaining()
            if remaining is not None and remaining <= 0:
                raise GitHubAPIError(
                    0, f"GitHub call ran out of its {self.budget:g}s time budget", timed_out=True
                )
            timeout = self.timeout if remaining is None else min(self.timeout, remaining)
            started = time.monotonic()
            try:
                response = self._session.request(
                    method,
                    self.base_url + path,
                    params=dict(params) if params else None,
                    json=json,
                    headers=headers,
                    timeout=timeout,
                )
            except requests.Timeout as e:  # before RequestException: ConnectTimeout is both
                raise GitHubAPIError(
                    0, f"GitHub did not answer within {timeout:g}s ({type(e).__name__})", timed_out=True
                ) from None
            except requests.RequestException as e:
                # str(e) can carry the URL (and so argument values): name the type only.
                raise GitHubAPIError(
                    0, f"network error talking to GitHub ({type(e).__name__})"
                ) from None
            logger.debug(
                "github: %s -> %s in %.0fms",
                method,
                response.status_code,
                (time.monotonic() - started) * 1000,
            )
            if response.status_code < 400:
                return response

            limit_wait = rate_limit_wait(response, now=self._clock())
            wait = limit_wait
            if wait is None and method == "GET" and response.status_code in (502, 503, 504):
                wait = _SERVER_ERROR_WAIT * (2**attempt)
            sleep_budget = self.max_retry_wait - waited
            remaining = self._remaining()
            if remaining is not None:
                sleep_budget = min(sleep_budget, remaining)
            if wait is None or attempt >= self.max_retries or wait > sleep_budget:
                raise GitHubAPIError(
                    response.status_code, error_message(response), retry_after=limit_wait
                )
            attempt += 1
            logger.warning(
                "github: HTTP %s, retrying in %.0fs (retry %d/%d)",
                response.status_code,
                wait,
                attempt,
                self.max_retries,
            )
            self._sleep(wait)
            waited += wait

    def get(self, path: str, params: Mapping[str, Any] | None = None, **kw: Any) -> requests.Response:
        return self.request("GET", path, params=params, **kw)


# --------------------------------------------------------------------------
# Response helpers
# --------------------------------------------------------------------------


def json_body(response: requests.Response) -> Any:
    """Parsed JSON body; ``None`` for an empty body (e.g. 204)."""
    if response.status_code == 204 or not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        raise GitHubAPIError(
            response.status_code, "GitHub returned a response that is not JSON"
        ) from None


def page_hint(response: requests.Response) -> tuple[bool, int | None]:
    """``(has_more, next_page)`` from the ``Link: <...>; rel="next"`` header.
    ``next_page`` is None when there is no next page or the next link is a
    cursor rather than a page number."""
    next_url = (response.links.get("next") or {}).get("url")
    if not next_url:
        return False, None
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(next_url).query)
    try:
        return True, int(query["page"][0])
    except (KeyError, IndexError, ValueError):
        return True, None


def rate_limit_wait(response: requests.Response, *, now: float) -> float | None:
    """Seconds GitHub asks us to wait, or None when ``response`` is not a
    rate limit. Follows GitHub's guidance: ``Retry-After`` first, then an
    exhausted primary limit's ``x-ratelimit-reset``, then one minute for a
    secondary limit that names no wait."""
    status = response.status_code
    if status not in (403, 429):
        return None
    headers = response.headers
    retry_after = headers.get("Retry-After")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            pass
    if headers.get("x-ratelimit-remaining") == "0":
        try:
            return max(0.0, float(headers.get("x-ratelimit-reset", "")) - now) + 1.0
        except ValueError:
            return _SECONDARY_LIMIT_WAIT
    if status == 429 or "rate limit" in error_message(response).lower():
        return _SECONDARY_LIMIT_WAIT
    return None


def error_message(response: requests.Response) -> str:
    """GitHub's error ``message`` plus up to five validation ``errors``."""
    try:
        body = response.json()
    except ValueError:
        body = None
    message = ""
    details: list[str] = []
    if isinstance(body, dict):
        message = str(body.get("message") or "")
        for item in (body.get("errors") or [])[:5]:
            if isinstance(item, dict):
                text = item.get("message") or " ".join(
                    str(item[k]) for k in ("resource", "field", "code") if item.get(k)
                )
            else:
                text = str(item)
            if text:
                details.append(str(text))
    message = message or response.reason or "HTTP error"
    if details:
        message += f" ({'; '.join(details)})"
    return message[:_MAX_MESSAGE]


# --------------------------------------------------------------------------
# OAuth App grant revocation
# --------------------------------------------------------------------------


def revoke_grant(
    *,
    client_id: str,
    client_secret: str,
    access_token: str,
    session: requests.Session | None = None,
    base_url: str = API_BASE,
    timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """``DELETE /applications/{client_id}/grant``: revoke the user's whole
    authorization of this OAuth App (every token it issued them), so the app
    disappears from their authorized apps. Authenticated with the app's
    client id/secret (HTTP Basic). True on 204; False on anything else.
    Never raises."""
    http = session if session is not None else requests.Session()
    try:
        url = f"{base_url.rstrip('/')}/applications/{urllib.parse.quote(client_id, safe='')}/grant"
        status = http.request(
            "DELETE",
            url,
            auth=(client_id, client_secret),
            json={"access_token": access_token},
            headers={
                "Accept": JSON_MEDIA,
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": USER_AGENT,
            },
            timeout=timeout,
        ).status_code
    except Exception as e:  # noqa: BLE001 — never raises (e.g. a non-latin-1 secret in Basic auth)
        logger.warning("github: grant revoke failed (%s)", type(e).__name__)
        return False
    finally:
        if session is None:
            http.close()
    if status == 204:
        return True
    logger.warning("github: grant revoke returned HTTP %s", status)
    return False
