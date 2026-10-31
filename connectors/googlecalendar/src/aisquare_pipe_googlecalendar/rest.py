"""Google Calendar v3 REST client for the Google Calendar toolkit.

This module is the package's only HTTP boundary (tests mock the
``requests.Session`` handed to it). One :class:`GoogleCalendarRestClient`
serves one tool call with one access token. The token rides the
``Authorization`` header only: it never appears in a URL, a log line, an
exception message or a repr.

Non-2xx answers raise :class:`GoogleCalendarAPIError` carrying the status,
Google's own message, its first error ``reason`` and whether the 403 was a
missing OAuth scope; the toolkit turns that into a failed ``ToolResult``.

Rate limits: a 429, or a 403 whose reason is ``rateLimitExceeded`` /
``userRateLimitExceeded``, is retried up to ``max_retries`` times with
exponential backoff (1s, 2s, 4s, ... — or ``Retry-After`` when Google sends
one). A 5xx on a GET is retried the same way; on a write it is not (an
event created half-way must not be created twice). The total sleep per call
is capped at ``max_retry_wait`` seconds; a longer wait fails at once (with
``GoogleCalendarAPIError.retry_after`` set).

Time budget: with ``budget`` (seconds; the host's ``config["timeout_seconds"]``)
the whole call — every attempt and every retry sleep — fits inside it. Each
request's timeout shrinks to what is left, a retry that would sleep past it
fails at once, and running out raises a ``timed_out`` error.
"""

from __future__ import annotations

import logging
import math
import time
import urllib.parse
from collections.abc import Callable, Mapping
from typing import Any, Self

import requests

logger = logging.getLogger("aisquare.pipe.googlecalendar")

API_BASE = "https://www.googleapis.com/calendar/v3"
USER_AGENT = "aisquare-pipe-googlecalendar"

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_MAX_RETRY_WAIT = 60.0

#: 403/429 reasons that mean "slow down" (retried); every other 403 is final.
RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})
#: Reasons Google gives for a token that lacks the scope a method needs.
SCOPE_REASONS = frozenset({"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"})
_RETRYABLE_SERVER_ERRORS = frozenset({500, 502, 503, 504})
_BACKOFF_BASE = 1.0
_MAX_MESSAGE = 500
#: Time a retry keeps for its request after a backoff sleep (budgeted calls).
_REQUEST_ROOM = 2.0


class GoogleCalendarAPIError(Exception):
    """A non-2xx Google answer (``status``) or a transport failure (``status=0``).

    ``message`` is Google's own error text; ``reason`` its first
    ``errors[].reason`` (``notFound``, ``requiredAccessLevel``,
    ``timeRangeEmpty``, ``userRateLimitExceeded``, ...); ``retry_after`` is set when the failure is
    a rate limit, in seconds; ``timed_out`` when Google did not answer in time
    (or the budget ran out); ``insufficient_scope`` when a 403 says the token
    was not granted the scope the method needs.
    """

    def __init__(
        self,
        status: int,
        message: str,
        *,
        reason: str | None = None,
        retry_after: float | None = None,
        timed_out: bool = False,
        insufficient_scope: bool = False,
    ) -> None:
        super().__init__(f"{status}: {message}" if status else message)
        self.status = status
        self.message = message
        self.reason = reason
        self.retry_after = retry_after
        self.timed_out = timed_out
        self.insufficient_scope = insufficient_scope


class GoogleCalendarRestClient:
    """Bearer-token client for the Calendar v3 API (one per call)."""

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
        data: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> requests.Response:
        """Send one request (``path`` is relative to the API base); return the
        2xx response or raise :class:`GoogleCalendarAPIError`."""
        base = self.base_url
        all_headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": USER_AGENT,
            **(headers or {}),
        }
        per_request = self.timeout if timeout is None else timeout
        waited = 0.0
        attempt = 0
        while True:
            remaining = self._remaining()
            if remaining is not None and remaining <= 0:
                raise GoogleCalendarAPIError(
                    0, f"Google Calendar call ran out of its {self.budget:g}s time budget", timed_out=True
                )
            request_timeout = per_request if remaining is None else min(per_request, remaining)
            started = time.monotonic()
            try:
                response = self._session.request(
                    method,
                    base + path,
                    params=dict(params) if params else None,
                    json=json,
                    data=data,
                    headers=dict(all_headers),
                    timeout=request_timeout,
                )
            except requests.Timeout as e:  # before RequestException: ConnectTimeout is both
                raise GoogleCalendarAPIError(
                    0,
                    f"Google Calendar did not answer within {round(request_timeout, 1):g}s ({type(e).__name__})",
                    timed_out=True,
                ) from None
            except requests.RequestException as e:
                # str(e) can carry the URL (and so argument values): name the type only.
                raise GoogleCalendarAPIError(0, f"network error talking to Google Calendar ({type(e).__name__})") from None
            logger.debug(
                "googlecalendar: %s -> %s in %.0fms",
                method,
                response.status_code,
                (time.monotonic() - started) * 1000,
            )
            if response.status_code < 400:
                return response

            limited = is_rate_limited(response)
            wait: float | None = None
            if limited or (method == "GET" and response.status_code in _RETRYABLE_SERVER_ERRORS):
                wait = retry_wait(response, attempt)
            remaining = self._remaining()
            fits = wait is not None and wait <= self.max_retry_wait - waited
            if fits and wait is not None and remaining is not None:
                # The retry itself needs time too: never sleep the budget away.
                fits = wait + min(per_request, _REQUEST_ROOM) <= remaining
            if wait is None or attempt >= self.max_retries or not fits:
                raise api_error(response, retry_after=wait if limited else None)
            attempt += 1
            logger.warning(
                "googlecalendar: HTTP %s, retrying in %.0fs (retry %d/%d)",
                response.status_code,
                wait,
                attempt,
                self.max_retries,
            )
            self._sleep(wait)
            waited += wait

    def get(self, path: str, params: Mapping[str, Any] | None = None, **kw: Any) -> requests.Response:
        return self.request("GET", path, params=params, **kw)

    def send(self, method: str, path: str, json: Any = None, **kw: Any) -> requests.Response:
        return self.request(method, path, json=json, **kw)

# --------------------------------------------------------------------------
# Request / response helpers
# --------------------------------------------------------------------------


def quote_segment(value: str, what: str = "Google Calendar id") -> str:
    """A calendar or event id as one URL path segment. Calendar ids are often
    email addresses, some with ``#`` (``en.usa#holiday@group.v.calendar.google.com``):
    anything outside ``[A-Za-z0-9_.~-]`` is encoded, and a
    ``.``/``..`` id — which the HTTP stack would resolve as a dot segment and
    so reroute the call — is refused."""
    text = str(value).strip()
    if text in ("", ".", ".."):
        raise ValueError(f"a {what} must not be empty, '.' or '..'")
    return urllib.parse.quote(text, safe="")


def json_body(response: requests.Response) -> Any:
    """Parsed JSON body; ``None`` for an empty body (e.g. 204)."""
    if response.status_code == 204 or not response.content:
        return None
    try:
        return response.json()
    except Exception:  # noqa: BLE001 — ValueError, or RecursionError on absurd nesting
        raise GoogleCalendarAPIError(response.status_code, "Google Calendar returned a response that is not JSON") from None


def json_object(response: requests.Response) -> dict[str, Any]:
    body = json_body(response)
    return body if isinstance(body, dict) else {}


def _error_payload(response: requests.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 — any unparseable body (RecursionError included) has no details
        return {}
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, dict) else {}


def error_details(response: requests.Response) -> tuple[str, str | None, bool]:
    """Google's error ``message``, first ``errors[].reason``, and whether the
    error says the token lacks a scope (an ``insufficientPermissions`` reason,
    an ``ACCESS_TOKEN_SCOPE_INSUFFICIENT`` ErrorInfo, or a
    ``WWW-Authenticate: ... error="insufficient_scope"`` header)."""
    error = _error_payload(response)
    message = str(error.get("message") or "")
    reason: str | None = None
    reasons: set[str] = set()
    errors = error.get("errors")
    if isinstance(errors, list):
        for item in errors:
            if isinstance(item, dict) and item.get("reason"):
                reasons.add(str(item["reason"]))
                if reason is None:
                    reason = str(item["reason"])
                    message = message or str(item.get("message") or "")
    details = error.get("details")
    if isinstance(details, list):
        reasons.update(str(d.get("reason")) for d in details if isinstance(d, dict) and d.get("reason"))
    challenge = str(response.headers.get("WWW-Authenticate") or "")
    scope = response.status_code == 403 and (
        bool(reasons & SCOPE_REASONS)
        or "insufficient_scope" in challenge
        or "insufficient authentication scopes" in message.lower()
    )
    message = message or response.reason or "HTTP error"
    return message[:_MAX_MESSAGE], reason, scope


def api_error(response: requests.Response, *, retry_after: float | None = None) -> GoogleCalendarAPIError:
    message, reason, scope = error_details(response)
    return GoogleCalendarAPIError(
        response.status_code,
        message,
        reason=reason,
        retry_after=retry_after,
        insufficient_scope=scope,
    )


def is_rate_limited(response: requests.Response) -> bool:
    """A 429, or a 403 whose reason is a rate limit (not a daily quota)."""
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    _, reason, _ = error_details(response)
    return reason in RATE_LIMIT_REASONS


def retry_wait(response: requests.Response, attempt: int) -> float:
    """Seconds to wait before retry ``attempt + 1``: ``Retry-After`` when
    Google sends a finite one, else exponential backoff (1s, 2s, 4s, ...)."""
    header = response.headers.get("Retry-After")
    if header:
        try:
            seconds = float(header)
        except ValueError:
            seconds = math.nan
        if math.isfinite(seconds):  # inf / 1e309 / nan would overflow callers' arithmetic
            return max(0.0, seconds)
    return _BACKOFF_BASE * (2**attempt)
