"""Hermetic HTTP for the GitHub toolkit tests.

``FakeSession`` stands in for the ``requests.Session`` the toolkit's REST
client sends everything through: it records each request and answers from a
script of real ``requests.Response`` objects (so ``.json()``, ``.links`` and
header lookups behave exactly as in production). No socket is ever opened.
"""

from __future__ import annotations

import http
import json
from dataclasses import dataclass, field
from typing import Any

import requests
from requests.structures import CaseInsensitiveDict

from aisquare_pipe_github import GitHubToolkit

TOKEN = "gho_s3cr3tT0kenValue123"
CONFIG = {"access_token": TOKEN}
#: What api.github.com sends for the raw media type (observed live).
RAW_CONTENT_TYPE = "application/vnd.github.raw+json; charset=utf-8"


@dataclass
class Call:
    method: str
    url: str
    params: dict[str, Any] | None
    json: Any
    headers: dict[str, str]
    timeout: Any = None
    auth: Any = None

    @property
    def path(self) -> str:
        return self.url.removeprefix("https://api.github.com")


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
                headers=dict(kwargs.get("headers") or {}),
                timeout=kwargs.get("timeout"),
                auth=kwargs.get("auth"),
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
    r.reason = http.HTTPStatus(status).phrase
    if content is None:
        content = b"" if body is None else json.dumps(body).encode()
        content_type = content_type or ("application/json; charset=utf-8" if body is not None else None)
    r._content = content
    all_headers = dict(headers or {})
    if content_type:
        all_headers.setdefault("Content-Type", content_type)
    r.headers = CaseInsensitiveDict(all_headers)
    r.encoding = "utf-8"
    r.url = "https://api.github.com/"
    return r


def next_link(page: int) -> dict[str, str]:
    """A ``Link`` header announcing page ``page`` (and a last page)."""
    base = "https://api.github.com/resource"
    return {
        "Link": f'<{base}?per_page=30&page={page}>; rel="next", <{base}?per_page=30&page=9>; rel="last"'
    }


@dataclass
class Harness:
    """A toolkit wired to a FakeSession with a recording, non-sleeping sleep."""

    session: FakeSession
    toolkit: GitHubToolkit
    sleeps: list[float]

    def call(self, slug: str, arguments: dict[str, Any] | None = None, config: dict[str, Any] | None = None):
        return self.toolkit.call_tool(slug, arguments or {}, CONFIG if config is None else config)


def harness(*responses: Any, **toolkit_kwargs: Any) -> Harness:
    session = FakeSession(list(responses))
    sleeps: list[float] = []
    toolkit = GitHubToolkit(session=session, sleep=sleeps.append, **toolkit_kwargs)  # type: ignore[arg-type]
    return Harness(session, toolkit, sleeps)
