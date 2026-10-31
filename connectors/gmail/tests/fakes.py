"""Hermetic HTTP and Gmail fixtures for the Gmail tests.

``FakeSession`` stands in for the ``requests.Session`` the REST client sends
everything through: it records each request and answers from a script of
real ``requests.Response`` objects (so ``.json()`` and header lookups behave
exactly as in production). No socket is ever opened.

The message builders produce Gmail's ``users.messages`` resource shape: a
``payload`` part tree whose bodies are base64url, as the API returns them.
"""

from __future__ import annotations

import base64
import http
import io
import json
from dataclasses import dataclass, field
from typing import Any

import requests
from requests.structures import CaseInsensitiveDict

from aisquare_pipe_gmail import GmailToolkit
from aisquare_pipe_gmail.rest import API_BASE, UPLOAD_BASE

TOKEN = "ya29.a0AfB_s3cr3tT0kenValue123"
CONFIG = {"access_token": TOKEN}
SCOPE_CHALLENGE = (
    'Bearer realm="https://accounts.google.com/", error="insufficient_scope", '
    'scope="https://www.googleapis.com/auth/gmail.send"'
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
    def upload(self) -> bool:
        return self.url.startswith(UPLOAD_BASE)

    @property
    def path(self) -> str:
        return self.url.removeprefix(UPLOAD_BASE).removeprefix(API_BASE)


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
                "metadata": {"service": "gmail.googleapis.com"},
            }
        ]
    return response(status, {"error": error}, headers=headers)


def unauthorized() -> requests.Response:
    return google_error(401, "Request had invalid authentication credentials.", "authError")


def scope_denied() -> requests.Response:
    """What Gmail answers a token without the method's scope."""
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
        google_error(status, "Too many concurrent requests for user.", "rateLimitExceeded", headers=headers)
        for _ in range(n)
    ]


# --------------------------------------------------------------------------
# Gmail resources
# --------------------------------------------------------------------------


def b64(data: bytes | str) -> str:
    raw = data.encode() if isinstance(data, str) else data
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def headers(**values: str) -> list[dict[str, str]]:
    """Gmail header list; ``Message_ID="<x>"`` becomes ``Message-ID``."""
    return [{"name": name.replace("_", "-"), "value": value} for name, value in values.items()]


def text_part(text: str, mime: str = "text/plain", charset: str = "UTF-8", part_id: str = "0") -> dict[str, Any]:
    raw = text.encode(charset)
    return {
        "partId": part_id,
        "mimeType": mime,
        "filename": "",
        "headers": headers(Content_Type=f'{mime}; charset="{charset}"'),
        "body": {"size": len(raw), "data": b64(raw)},
    }


def attachment_part(
    filename: str, mime: str, attachment_id: str, size: int = 1234, part_id: str = "1", inline: bool = False
) -> dict[str, Any]:
    disposition = "inline" if inline else "attachment"
    return {
        "partId": part_id,
        "mimeType": mime,
        "filename": filename,
        "headers": headers(Content_Type=mime, Content_Disposition=f'{disposition}; filename="{filename}"'),
        "body": {"attachmentId": attachment_id, "size": size},
    }


def message(
    message_id: str = "18c1a2b3c4d5e6f7",
    *,
    thread_id: str = "18c1a2b3c4d5e6f0",
    subject: str = "Q3 report",
    sender: str = "Ann <ann@example.com>",
    to: str = "me@example.com",
    body: str | None = "Hello there",
    parts: list[dict[str, Any]] | None = None,
    labels: tuple[str, ...] = ("INBOX", "UNREAD"),
    internal_date: str = "1793433600000",
    snippet: str = "Hello there",
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """A ``format=full`` message: ``multipart/mixed`` when ``parts`` is
    given, else a single ``text/plain`` part (no body when ``body`` is None)."""
    top = headers(From=sender, To=to, Subject=subject, Date="Fri, 30 Oct 2026 10:00:00 +0000", **(extra_headers or {}))
    if parts is not None:
        payload: dict[str, Any] = {
            "partId": "",
            "mimeType": "multipart/mixed",
            "filename": "",
            "headers": top,
            "body": {"size": 0},
            "parts": parts,
        }
    else:
        payload = text_part(body or "", part_id="")
        payload["headers"] = top + payload["headers"]
        if body is None:
            payload["body"] = {"size": 0}
    return {
        "id": message_id,
        "threadId": thread_id,
        "labelIds": list(labels),
        "snippet": snippet,
        "historyId": "99001",
        "internalDate": internal_date,
        "sizeEstimate": 4321,
        "payload": payload,
    }


def listing(
    ids: list[str], *, key: str = "messages", token: str | None = None, estimate: int | None = None
) -> dict[str, Any]:
    out: dict[str, Any] = {key: [{"id": i, "threadId": f"t-{i}"} for i in ids]}
    if token:
        out["nextPageToken"] = token
    out["resultSizeEstimate"] = len(ids) if estimate is None else estimate
    return out


@dataclass
class Harness:
    """A toolkit wired to a FakeSession with a recording, non-sleeping sleep."""

    session: FakeSession
    toolkit: GmailToolkit
    sleeps: list[float]

    def call(self, slug: str, arguments: dict[str, Any] | None = None, config: dict[str, Any] | None = None):
        return self.toolkit.call_tool(slug, arguments or {}, CONFIG if config is None else config)


def harness(*responses: Any, **toolkit_kwargs: Any) -> Harness:
    session = FakeSession(list(responses))
    sleeps: list[float] = []
    toolkit = GmailToolkit(session=session, sleep=sleeps.append, **toolkit_kwargs)  # type: ignore[arg-type]
    return Harness(session, toolkit, sleeps)


def raw_message(call: Call) -> bytes:
    """The RFC 2822 bytes a send/draft request carried (JSON or upload)."""
    if call.upload:
        body: bytes = call.data
        start = body.index(b"Content-Type: message/rfc822\r\n\r\n") + len(b"Content-Type: message/rfc822\r\n\r\n")
        end = body.rindex(b"\r\n--")
        return body[start:end]
    payload = call.json.get("message", call.json)
    return base64.urlsafe_b64decode(payload["raw"] + "=" * (-len(payload["raw"]) % 4))
