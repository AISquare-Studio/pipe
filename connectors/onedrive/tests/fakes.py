"""Hermetic HTTP for the OneDrive toolkit tests.

``FakeSession`` stands in for the ``requests.Session`` the toolkit's Graph
client sends everything through: it records each request and answers from a
script of real ``requests.Response`` objects (so ``.json()``, ``.content``
and header lookups behave exactly as in production). No socket is ever
opened.
"""

from __future__ import annotations

import http
import json
from dataclasses import dataclass, field
from typing import Any

import requests
from requests.structures import CaseInsensitiveDict

from aisquare_pipe_onedrive import OneDriveToolkit

GRAPH = "https://graph.microsoft.com/v1.0"
TOKEN = "EwBIA8l6BAAUs3cr3tT0kenValue123"
CONFIG = {"access_token": TOKEN}
UPLOAD_URL = "https://api.onedrive.com/rup/a1b2c3d4e5/eyJzZXNzaW9uIn0?cid=1234"
MONITOR_URL = "https://api.onedrive.com/v1.0/monitor/4A3407B5-88FC-4504-8B21-0AABD3412717"


@dataclass
class Call:
    method: str
    url: str
    params: dict[str, Any] | None
    json: Any
    data: Any
    headers: dict[str, str]
    timeout: Any = None
    allow_redirects: Any = None

    @property
    def path(self) -> str:
        return self.url.removeprefix(GRAPH)


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
                allow_redirects=kwargs.get("allow_redirects"),
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
    """A real ``requests.Response``: JSON ``body`` or raw ``content``."""
    r = requests.Response()
    r.status_code = status
    r.reason = http.HTTPStatus(status).phrase if status in http.HTTPStatus._value2member_map_ else "Bandwidth Limit Exceeded"
    if content is None:
        content = b"" if body is None else json.dumps(body).encode()
        content_type = content_type or ("application/json" if body is not None else None)
    r._content = content
    all_headers = dict(headers or {})
    if content_type:
        all_headers.setdefault("Content-Type", content_type)
    r.headers = CaseInsensitiveDict(all_headers)
    r.encoding = "utf-8"
    r.url = GRAPH + "/"
    return r


def graph_error(status: int, code: str, message: str, **kw: Any) -> requests.Response:
    """Graph's error body: ``{"error": {"code", "message", "innerError"}}``."""
    body = {"error": {"code": code, "message": message, "innerError": {"request-id": "r-1"}}}
    return response(status, body, **kw)


def listing(items: list[Any], next_link: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"@odata.context": f"{GRAPH}/$metadata#Collection(driveItem)", "value": items}
    if next_link:
        body["@odata.nextLink"] = next_link
    return body


def file_item(name: str = "report.pdf", item_id: str = "01ABCDEFGHIJKLMNOPQRSTUVWXYZ234567", **extra: Any) -> dict[str, Any]:
    return {
        "@microsoft.graph.downloadUrl": "https://public.am.files.1drv.com/y4mSECRETdownload",
        "id": item_id,
        "name": name,
        "size": 2048,
        "webUrl": f"https://contoso-my.sharepoint.com/personal/ana/Documents/{name}",
        "lastModifiedDateTime": "2026-09-30T12:00:00Z",
        "file": {"mimeType": "application/pdf", "hashes": {"quickXorHash": "abc="}},
        "parentReference": {"driveId": "b!drive123", "id": "01PARENT", "path": "/drive/root:/Documents"},
        **extra,
    }


def folder_item(name: str = "Documents", item_id: str = "01FOLDERABCDEFGHIJKLMNOPQRSTUVWXY") -> dict[str, Any]:
    return {
        "id": item_id,
        "name": name,
        "folder": {"childCount": 3},
        "webUrl": f"https://contoso-my.sharepoint.com/personal/ana/Documents/{name}",
        "parentReference": {"driveId": "b!drive123", "path": "/drive/root:"},
    }


@dataclass
class Harness:
    """A toolkit wired to a FakeSession with a recording, non-sleeping sleep."""

    session: FakeSession
    toolkit: OneDriveToolkit
    sleeps: list[float]

    def call(self, slug: str, arguments: dict[str, Any] | None = None, config: dict[str, Any] | None = None):
        return self.toolkit.call_tool(slug, arguments or {}, CONFIG if config is None else config)


def harness(*responses: Any, **toolkit_kwargs: Any) -> Harness:
    session = FakeSession(list(responses))
    sleeps: list[float] = []
    toolkit = OneDriveToolkit(session=session, sleep=sleeps.append, **toolkit_kwargs)  # type: ignore[arg-type]
    return Harness(session, toolkit, sleeps)
