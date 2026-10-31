"""Hermetic HTTP and Calendar fixtures for the Google Calendar tests.

``FakeSession`` stands in for the ``requests.Session`` the REST client sends
everything through: it records each request and answers from a script of
real ``requests.Response`` objects (so ``.json()`` and header lookups behave
exactly as in production). No socket is ever opened.

The builders produce Calendar's resource shapes (events, event pages,
calendar-list entries).
"""

from __future__ import annotations

import http
import io
import json
from dataclasses import dataclass, field
from typing import Any

import requests
from requests.structures import CaseInsensitiveDict

from aisquare_pipe_googlecalendar import GoogleCalendarToolkit
from aisquare_pipe_googlecalendar.rest import API_BASE

TOKEN = "ya29.a0AfB_s3cr3tT0kenValue123"
CONFIG = {"access_token": TOKEN}
SCOPE_CHALLENGE = (
    'Bearer realm="https://accounts.google.com/", error="insufficient_scope", '
    'scope="https://www.googleapis.com/auth/calendar.events"'
)


@dataclass
class Call:
    method: str
    url: str
    params: dict[str, Any] | None
    json: Any
    data: Any
    headers: dict[str, str]
    timeout: Any = None

    @property
    def path(self) -> str:
        return self.url.removeprefix(API_BASE)


@dataclass
class FakeSession:
    responses: list[Any] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    closed: bool = False

    def queue(self, *responses: Any) -> FakeSession:
        self.responses.extend(responses)
        return self

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append(
            Call(
                method=method,
                url=url,
                params=kwargs.get("params"),
                json=kwargs.get("json"),
                data=kwargs.get("data"),
                headers=dict(kwargs.get("headers") or {}),
                timeout=kwargs.get("timeout"),
            )
        )
        if not self.responses:
            raise AssertionError(f"unexpected request: {method} {url}")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        self.closed = True

    @property
    def last(self) -> Call:
        return self.calls[-1]


def response(
    status: int = 200,
    body: Any = None,
    *,
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
    content_type: str | None = None,
) -> requests.Response:
    """A real ``requests.Response`` with a JSON ``body`` or raw ``content``."""
    r = requests.Response()
    r.status_code = status
    r.reason = http.HTTPStatus(status).phrase
    if content is None:
        content = b"" if body is None else json.dumps(body).encode()
        content_type = content_type or ("application/json; charset=UTF-8" if body is not None else None)
    r._content = content
    r.raw = io.BytesIO(content)
    all_headers = dict(headers or {})
    if content_type:
        all_headers.setdefault("Content-Type", content_type)
    r.headers = CaseInsensitiveDict(all_headers)
    r.encoding = "utf-8"
    r.url = API_BASE
    return r


def google_error(
    status: int,
    message: str,
    reason: str,
    *,
    headers: dict[str, str] | None = None,
    info_reason: str | None = None,
) -> requests.Response:
    """Google's error body: ``{"error": {"code", "message", "errors": [{"reason"}], "details"}}``."""
    error: dict[str, Any] = {
        "code": status,
        "message": message,
        "errors": [{"domain": "global", "reason": reason, "message": message}],
    }
    if info_reason:
        error["status"] = "PERMISSION_DENIED"
        error["details"] = [
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": info_reason,
                "domain": "googleapis.com",
                "metadata": {"service": "calendar-json.googleapis.com"},
            }
        ]
    return response(status, {"error": error}, headers=headers)


def unauthorized() -> requests.Response:
    return google_error(401, "Request had invalid authentication credentials.", "authError")


def scope_denied() -> requests.Response:
    """What Calendar answers a token without the method's scope."""
    return google_error(
        403,
        "Request had insufficient authentication scopes.",
        "insufficientPermissions",
        headers={"WWW-Authenticate": SCOPE_CHALLENGE},
        info_reason="ACCESS_TOKEN_SCOPE_INSUFFICIENT",
    )


def rate_limited(n: int = 1, *, status: int = 429, retry_after: str | None = None) -> list[requests.Response]:
    headers = {"Retry-After": retry_after} if retry_after is not None else None
    return [
        google_error(status, "Rate Limit Exceeded", "rateLimitExceeded", headers=headers)
        for _ in range(n)
    ]


# --------------------------------------------------------------------------
# Calendar resources
# --------------------------------------------------------------------------


def event(
    event_id: str = "evt123",
    *,
    summary: str = "Standup",
    start: str = "2026-11-02T09:00:00-05:00",
    end: str = "2026-11-02T09:30:00-05:00",
    zone: str | None = "America/New_York",
    **extra: Any,
) -> dict[str, Any]:
    """An events resource; a bare date for ``start`` makes it all-day."""
    def moment(value: str) -> dict[str, str]:
        if len(value) == 10:
            return {"date": value}
        out = {"dateTime": value}
        if zone:
            out["timeZone"] = zone
        return out

    return {
        "kind": "calendar#event",
        "etag": '"3181161784712000"',
        "id": event_id,
        "status": "confirmed",
        "htmlLink": f"https://www.google.com/calendar/event?eid={event_id}",
        "summary": summary,
        "start": moment(start),
        "end": moment(end),
        **extra,
    }


def events_page(*events: dict[str, Any], token: str | None = None, zone: str = "America/New_York", sync: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": "calendar#events", "summary": "me@example.com", "timeZone": zone, "items": list(events)}
    if token:
        out["nextPageToken"] = token
    if sync:
        out["nextSyncToken"] = sync
    return out


def calendar_entry(calendar_id: str, summary: str, *, primary: bool = False, zone: str = "America/New_York", role: str = "owner") -> dict[str, Any]:
    entry: dict[str, Any] = {"kind": "calendar#calendarListEntry", "id": calendar_id, "summary": summary, "timeZone": zone, "accessRole": role}
    if primary:
        entry["primary"] = True
    return entry


def zone_reply(zone: str = "America/New_York") -> requests.Response:
    """What ``GET /calendars/{id}?fields=timeZone`` answers."""
    return response(200, {"timeZone": zone})


@dataclass
class Harness:
    """A toolkit wired to a FakeSession with a recording, non-sleeping sleep."""

    session: FakeSession
    toolkit: GoogleCalendarToolkit
    sleeps: list[float]

    def call(self, slug: str, arguments: dict[str, Any] | None = None, config: dict[str, Any] | None = None):
        return self.toolkit.call_tool(slug, arguments or {}, CONFIG if config is None else config)


def harness(*responses: Any, **toolkit_kwargs: Any) -> Harness:
    session = FakeSession(list(responses))
    sleeps: list[float] = []
    toolkit = GoogleCalendarToolkit(session=session, sleep=sleeps.append, **toolkit_kwargs)  # type: ignore[arg-type]
    return Harness(session, toolkit, sleeps)
