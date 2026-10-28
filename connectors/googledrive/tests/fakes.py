"""Hermetic HTTP for the Google Drive tests.

``FakeSession`` stands in for the ``requests.Session`` the REST client sends
everything through: it records each request and answers from a script of
real ``requests.Response`` objects (so ``.json()`` and header lookups behave
exactly as in production). No socket is ever opened.
"""

from __future__ import annotations

import http
import io
import json
from dataclasses import dataclass, field
from typing import Any

import requests
from requests.structures import CaseInsensitiveDict

from aisquare_pipe_googledrive import GoogleDriveToolkit
from aisquare_pipe_googledrive.rest import API_BASE, UPLOAD_BASE

TOKEN = "ya29.a0AfB_s3cr3tT0kenValue123"
CONFIG = {"access_token": TOKEN}
SESSION_URL = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=xyz"


@dataclass
class Call:
    method: str
    url: str
    params: dict[str, Any] | None
    json: Any
    data: Any
    headers: dict[str, str]
    timeout: Any = None
    stream: bool = False
    allow_redirects: bool = True

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
                stream=bool(kwargs.get("stream")),
                allow_redirects=kwargs.get("allow_redirects", True),
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
    """A real ``requests.Response``: JSON ``body`` or raw ``content``. The raw
    stream is set too, so ``stream=True`` reads work."""
    r = requests.Response()
    r.status_code = status
    r.reason = "Resume Incomplete" if status == 308 else http.HTTPStatus(status).phrase
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


def drive_error(status: int, message: str, reason: str, *, headers: dict[str, str] | None = None) -> requests.Response:
    """Drive's error body: ``{"error": {"code", "message", "errors": [{"reason"}]}}``."""
    body = {
        "error": {
            "code": status,
            "message": message,
            "errors": [{"domain": "global", "reason": reason, "message": message}],
        }
    }
    return response(status, body, headers=headers)


def rate_limited(n: int = 1, *, status: int = 403, reason: str = "userRateLimitExceeded") -> list[requests.Response]:
    return [drive_error(status, "User Rate Limit Exceeded", reason) for _ in range(n)]


def file_meta(file_id: str = "file123", name: str = "notes.txt", mime: str = "text/plain", size: int | None = 5, **extra: Any) -> dict[str, Any]:
    meta: dict[str, Any] = {"kind": "drive#file", "id": file_id, "name": name, "mimeType": mime, **extra}
    if size is not None:
        meta["size"] = str(size)
    return meta


@dataclass
class Harness:
    """A toolkit wired to a FakeSession with a recording, non-sleeping sleep."""

    session: FakeSession
    toolkit: GoogleDriveToolkit
    sleeps: list[float]

    def call(self, slug: str, arguments: dict[str, Any] | None = None, config: dict[str, Any] | None = None):
        return self.toolkit.call_tool(slug, arguments or {}, CONFIG if config is None else config)


def harness(*responses: Any, **toolkit_kwargs: Any) -> Harness:
    session = FakeSession(list(responses))
    sleeps: list[float] = []
    toolkit = GoogleDriveToolkit(session=session, sleep=sleeps.append, **toolkit_kwargs)  # type: ignore[arg-type]
    return Harness(session, toolkit, sleeps)
