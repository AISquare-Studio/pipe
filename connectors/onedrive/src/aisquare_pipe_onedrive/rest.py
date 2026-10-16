"""Microsoft Graph client for :class:`~aisquare_pipe_onedrive.toolkit.OneDriveToolkit`.

This module is the toolkit's only HTTP boundary (tests mock the
``requests.Session`` handed to it). One :class:`OneDriveRestClient` serves
one tool call with one delegated access token. The token rides the
``Authorization`` header of Graph requests only: it never appears in a URL,
a log line, an exception message or a repr, and it is never sent to the
pre-authenticated URLs Graph hands out (upload sessions, copy monitors) —
those calls go out with ``authenticated=False``.

Non-2xx answers raise :class:`OneDriveAPIError` carrying the status and
Graph's own error code and message; the toolkit turns that into a failed
``ToolResult``.

Throttling: a 429 (or a 503/509 with ``Retry-After``) is retried up to
``max_retries`` times, sleeping for what Graph asks. The total sleep per call
is capped at ``max_retry_wait`` seconds; a longer wait fails at once (with
``OneDriveAPIError.retry_after`` set). A 502/503/504 on a GET is retried with
a short backoff.

Time budget: with ``budget`` (seconds; the host's ``config["timeout_seconds"]``)
the whole call — every attempt and every retry sleep — fits inside it. Each
request's timeout shrinks to what is left, a retry that would sleep past it
fails at once, and running out raises a ``timed_out`` error.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any, Self

import requests

from aisquare_pipe_onedrive.constants import GRAPH_BASE_URL

logger = logging.getLogger("aisquare.pipe.onedrive")

USER_AGENT = "aisquare-pipe-onedrive"

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_MAX_RETRY_WAIT = 60.0

#: Statuses Graph uses for throttling (509: SharePoint bandwidth limit).
_THROTTLE_STATUSES = (429, 503, 509)
#: Wait for a 429/509 that names no ``Retry-After``.
_THROTTLE_WAIT = 5.0
#: Backoff for a 502/503/504 on a GET (safe to repeat).
_SERVER_ERROR_WAIT = 1.0
_MAX_MESSAGE = 500


class OneDriveAPIError(Exception):
    """A non-2xx Graph answer (``status``) or a transport failure (``status=0``).

    ``message`` is Graph's error text, ``code`` Graph's error code
    (``itemNotFound``, ``InvalidAuthenticationToken``, ...); ``retry_after``
    is set when the failure is throttling, in seconds; ``timed_out`` when
    Graph did not answer in time (or the budget ran out).
    """

    def __init__(
        self,
        status: int,
        message: str,
        *,
        code: str | None = None,
        retry_after: float | None = None,
        timed_out: bool = False,
    ) -> None:
        super().__init__(f"{status}: {message}" if status else message)
        self.status = status
        self.message = message
        self.code = code
        self.retry_after = retry_after
        self.timed_out = timed_out

    @property
    def rate_limited(self) -> bool:
        return self.retry_after is not None


class OneDriveRestClient:
    """Bearer-token client for Microsoft Graph (one per tool call)."""

    def __init__(
        self,
        token: str,
        *,
        session: requests.Session | None = None,
        base_url: str = GRAPH_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_retry_wait: float = DEFAULT_MAX_RETRY_WAIT,
        budget: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
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

    def remaining(self) -> float | None:
        """Seconds left in the budget (None: unbounded)."""
        if self._deadline is None:
            return None
        return self._deadline - self._monotonic()

    def sleep(self, seconds: float) -> None:
        self._sleep(seconds)

    # ------------------------------------------------------------------ http

    def _url(self, target: str, authenticated: bool) -> str:
        if target.startswith("/"):
            return self.base_url + target
        # Absolute URLs are Graph's pre-authenticated ones (upload sessions,
        # copy monitors): https only, and never with the bearer token.
        if authenticated or not target.startswith("https://"):
            raise ValueError("absolute URLs must be https and unauthenticated")
        return target

    def request(
        self,
        method: str,
        target: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        data: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        authenticated: bool = True,
        allow_redirects: bool = True,
    ) -> requests.Response:
        """Send one request and return the 2xx/3xx response or raise
        :class:`OneDriveAPIError`. ``target`` is a path under ``base_url``
        (``/me/drive``) or, with ``authenticated=False``, an absolute https
        URL Graph handed out."""
        url = self._url(target, authenticated)
        all_headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if authenticated:
            all_headers["Authorization"] = f"Bearer {self._token}"
        if headers:
            all_headers.update(headers)
        waited = 0.0
        attempt = 0
        while True:
            remaining = self.remaining()
            if remaining is not None and remaining <= 0:
                raise OneDriveAPIError(
                    0, f"OneDrive call ran out of its {self.budget:g}s time budget", timed_out=True
                )
            timeout = self.timeout if remaining is None else min(self.timeout, remaining)
            started = time.monotonic()
            try:
                response = self._session.request(
                    method,
                    url,
                    params=dict(params) if params else None,
                    json=json,
                    data=data,
                    headers=all_headers,
                    timeout=timeout,
                    allow_redirects=allow_redirects,
                )
            except requests.Timeout as e:  # before RequestException: ConnectTimeout is both
                raise OneDriveAPIError(
                    0,
                    f"Microsoft Graph did not answer within {timeout:.3g}s ({type(e).__name__})",
                    timed_out=True,
                ) from None
            except requests.RequestException as e:
                # str(e) can carry the URL (and so item paths): name the type only.
                raise OneDriveAPIError(
                    0, f"network error talking to Microsoft Graph ({type(e).__name__})"
                ) from None
            logger.debug(
                "onedrive: %s -> %s in %.0fms",
                method,
                response.status_code,
                (time.monotonic() - started) * 1000,
            )
            if response.status_code < 400:
                return response

            limit_wait = throttle_wait(response)
            wait = limit_wait
            if wait is None and method == "GET" and response.status_code in (502, 503, 504):
                wait = _SERVER_ERROR_WAIT * (2**attempt)
            sleep_budget = self.max_retry_wait - waited
            remaining = self.remaining()
            if remaining is not None:
                sleep_budget = min(sleep_budget, remaining)
            if wait is None or attempt >= self.max_retries or wait > sleep_budget:
                code, message = error_detail(response)
                raise OneDriveAPIError(
                    response.status_code, message, code=code, retry_after=limit_wait
                )
            attempt += 1
            logger.warning(
                "onedrive: HTTP %s, retrying in %.0fs (retry %d/%d)",
                response.status_code,
                wait,
                attempt,
                self.max_retries,
            )
            self._sleep(wait)
            waited += wait

    def get(self, target: str, params: Mapping[str, Any] | None = None, **kw: Any) -> requests.Response:
        return self.request("GET", target, params=params, **kw)


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
        raise OneDriveAPIError(
            response.status_code, "Microsoft Graph returned a response that is not JSON"
        ) from None


def throttle_wait(response: requests.Response) -> float | None:
    """Seconds Graph asks us to wait, or None when ``response`` is not
    throttling. A 429/509 is throttling with or without ``Retry-After``; a
    503 only when it carries one (otherwise it is a plain outage)."""
    status = response.status_code
    if status not in _THROTTLE_STATUSES:
        return None
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            pass
    return None if status == 503 else _THROTTLE_WAIT


def error_detail(response: requests.Response) -> tuple[str | None, str]:
    """``(code, message)`` from Graph's ``{"error": {"code", "message"}}``
    body; the HTTP reason when the body is not that shape."""
    try:
        body = response.json()
    except ValueError:
        body = None
    code: str | None = None
    message = ""
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        code = str(error.get("code") or "") or None
        message = str(error.get("message") or "")
    elif isinstance(error, str):  # OAuth-style {"error": "...", "error_description": "..."}
        code = error
        message = str(body.get("error_description") or "") if isinstance(body, dict) else ""
    message = message or response.reason or "HTTP error"
    if code:
        message = f"{code}: {message}"
    return code, message[:_MAX_MESSAGE]
