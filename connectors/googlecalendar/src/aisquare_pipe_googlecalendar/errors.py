"""Google Calendar failures as canonical ``ToolResult`` errors."""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

from aisquare import pipe
from aisquare.pipe import (
    ERROR_FORBIDDEN,
    ERROR_RATE_LIMITED,
    ERROR_TIMEOUT,
    ERROR_UPSTREAM_ERROR,
    ToolResult,
    error_code_for_status,
)

from aisquare_pipe_googlecalendar.rest import GoogleCalendarAPIError

#: Core's ``insufficient_scope`` error code once core has one, else
#: ``forbidden`` — a 403 for a missing scope maps to it either way.
ERROR_INSUFFICIENT_SCOPE: str = getattr(pipe, "ERROR_INSUFFICIENT_SCOPE", ERROR_FORBIDDEN)

_SCOPE_BASE = "https://www.googleapis.com/auth/"


def _short(scopes: Iterable[str]) -> str:
    return ", ".join(s.removeprefix(_SCOPE_BASE) for s in scopes)


def whole_seconds(value: float | None) -> float | None:
    """``value`` rounded up to whole seconds; None when absent or not finite."""
    if value is None or not math.isfinite(value):
        return None
    return float(math.ceil(value))


def describe_error(error: GoogleCalendarAPIError, scopes: Iterable[str] = ()) -> str:
    """``"<status>: <Google's message>"`` plus what the caller should do."""
    if not error.status:
        return error.message
    text = f"{error.status}: {error.message}"
    if error.status == 401:
        return (
            f"{text} — Google rejected this connection's access token (expired or revoked); "
            "refresh it, or the Google Calendar connection must be reconnected"
        )
    if error.retry_after is not None:
        seconds = whole_seconds(error.retry_after)
        when = f"in about {seconds:.0f}s" if seconds is not None else "later"
        return f"{text} — Google Calendar rate limit; retry {when}"
    if error.insufficient_scope:
        needed = _short(scopes)
        return (
            f"{text} — this Google Calendar connection was not granted the access this tool needs"
            + (f" ({needed})" if needed else "")
            + "; reconnect Google Calendar and approve it"
        )
    if error.status == 403:
        return (
            f"{text} — this Google Calendar connection is not allowed to do that (no write access "
            "to that calendar, a quota, or a domain policy)"
        )
    if error.status in (404, 410):
        return f"{text} — it does not exist, or this Google Calendar connection cannot see it"
    return text


def error_code(error: GoogleCalendarAPIError) -> str:
    """The canonical ``ToolResult.error_code`` for a Calendar failure: rate
    limits (429, or a 403 with reason ``rateLimitExceeded`` /
    ``userRateLimitExceeded``) are ``rate_limited``, a 403 for a missing
    OAuth scope is :data:`ERROR_INSUFFICIENT_SCOPE` and client-side timeouts
    ``timeout``; HTTP statuses map through core's ``error_code_for_status``
    (401 unauthorized, any other 403 forbidden, 404/410 not_found, 400
    invalid_arguments, 409/412 conflict, ...); network failures and 5xx are
    ``upstream_error``."""
    if error.retry_after is not None:
        return ERROR_RATE_LIMITED
    if error.timed_out:
        return ERROR_TIMEOUT
    if not error.status:
        return ERROR_UPSTREAM_ERROR
    if error.status == 403 and error.insufficient_scope:
        return ERROR_INSUFFICIENT_SCOPE
    return error_code_for_status(error.status)


def failure(error: GoogleCalendarAPIError, scopes: Iterable[str] = ()) -> ToolResult:
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
