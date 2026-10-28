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
``max_retries`` times, sleeping for what Graph asks (a ``Retry-After`` that is
not a finite, non-negative number counts as absent; one above
:data:`MAX_RETRY_AFTER` is capped). The total sleep per call is capped at
``max_retry_wait`` seconds; a longer wait fails at once (with
``OneDriveAPIError.retry_after`` set). A 502/503/504 on a GET is retried with
a short backoff.

Time budget: with ``budget`` (seconds; the host's ``config["timeout_seconds"]``)
the whole call — every attempt, every retry sleep, every downloaded chunk —
fits inside it. Each request's timeout shrinks to what is left; a retry whose
sleep would not leave ``min(timeout, 2s)`` for the retried request fails at
once (as throttling, with ``retry_after``); running out raises a
``timed_out`` error. Downloads (:meth:`OneDriveRestClient.download_bytes`)
are streamed with a byte cap and a budget check between chunks, because a
request timeout bounds each read, not the whole body.
"""

from __future__ import annotations

import logging
import math
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
#: Time a retried request needs after a sleep; a sleep that would leave less
#: of the budget fails at once instead (throttling, with retry_after).
_REQUEST_FLOOR = 2.0
#: Longest ``Retry-After`` taken at face value (seconds); larger is capped.
MAX_RETRY_AFTER = 3600
#: Bytes per read of a streamed download.
_READ_CHUNK = 64 * 1024
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


class DownloadLimitError(OneDriveAPIError):
    """A download grew past the caller's byte cap (the rest was not read)."""

    def __init__(self, limit: int) -> None:
        super().__init__(0, f"the file is larger than the {limit:,}-byte download limit")
        self.limit = limit


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
        stream: bool = False,
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
                    stream=stream,
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
            remaining = self.remaining()
            if (
                wait is None
                or attempt >= self.max_retries
                or waited + wait > self.max_retry_wait
                # The sleep must leave room for the retried request itself.
                or (remaining is not None and wait + min(self.timeout, _REQUEST_FLOOR) > remaining)
            ):
                code, message = error_detail(response)
                if stream:
                    response.close()
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
            if stream:
                response.close()
            self._sleep(wait)
            waited += wait

    def download_bytes(
        self,
        target: str,
        params: Mapping[str, Any] | None = None,
        *,
        max_bytes: int,
    ) -> bytes:
        """GET ``target`` as a stream and read it in chunks: more than
        ``max_bytes`` raises :class:`DownloadLimitError` (the rest is never
        read), and a spent time budget raises a ``timed_out`` error between
        chunks. The response is always closed."""
        response = self.request("GET", target, params=params, headers={"Accept": "*/*"}, stream=True)
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in response.iter_content(_READ_CHUNK):
                total += len(chunk)
                if total > max_bytes:
                    raise DownloadLimitError(max_bytes)
                remaining = self.remaining()
                if remaining is not None and remaining <= 0:
                    raise OneDriveAPIError(
                        0, f"OneDrive download ran out of its {self.budget:g}s time budget", timed_out=True
                    )
                chunks.append(chunk)
        except requests.Timeout as e:  # before RequestException: ConnectTimeout is both
            raise OneDriveAPIError(
                0, f"OneDrive stopped sending the file ({type(e).__name__})", timed_out=True
            ) from None
        except requests.RequestException as e:
            raise OneDriveAPIError(
                0, f"network error downloading from OneDrive ({type(e).__name__})"
            ) from None
        finally:
            response.close()
        return b"".join(chunks)

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
    except Exception:  # noqa: BLE001 — not JSON, or too deeply nested to parse
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
    seconds = retry_after_seconds(response.headers.get("Retry-After"))
    if seconds is not None:
        return seconds
    return None if status == 503 else _THROTTLE_WAIT


def retry_after_seconds(value: Any) -> float | None:
    """A ``Retry-After`` header in seconds, capped at :data:`MAX_RETRY_AFTER`;
    None when it is missing or not a finite, non-negative number (``inf``,
    ``1e400``, ``nan``, ``-5``, an HTTP date). Never raises."""
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, float(MAX_RETRY_AFTER))


def error_detail(response: requests.Response) -> tuple[str | None, str]:
    """``(code, message)`` from Graph's ``{"error": {"code", "message"}}``
    body; the HTTP reason when the body is not that shape.

    Never raises, whatever the body holds (any JSON shape, or none): it runs
    inside ``request()`` before the status is captured, so a parse error here
    would turn a 401/404/429 into an unclassified failure."""
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 — not JSON, or too deeply nested to parse
        body = None
    code: str | None = None
    message = ""
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        code = _text(error.get("code")) or None
        message = _text(error.get("message"))
    elif isinstance(error, str):  # OAuth-style {"error": "...", "error_description": "..."}
        code = error or None
        message = _text(body.get("error_description")) if isinstance(body, dict) else ""
    message = message or response.reason or "HTTP error"
    if code:
        message = f"{code}: {message}"
    return code, message[:_MAX_MESSAGE]


def _text(value: Any) -> str:
    # Strings only: str() of an arbitrarily nested value can itself recurse.
    return value if isinstance(value, str) else ""
