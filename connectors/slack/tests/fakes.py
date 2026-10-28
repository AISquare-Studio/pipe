"""Hermetic HTTP for the Slack toolkit tests.

``FakeSession`` stands in for the ``requests.Session`` the toolkit's Web API
client sends everything through: it records each request and answers from a
script of real ``requests.Response`` objects (so ``.json()`` and header
lookups behave exactly as in production). No socket is ever opened.
"""

from __future__ import annotations

import http
import json
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import requests
from requests.structures import CaseInsensitiveDict

from aisquare_pipe_slack import SlackToolkit

USER_TOKEN = "xoxp-1111-2222-s3cr3tUserT0ken"
BOT_TOKEN = "xoxb-1111-2222-s3cr3tB0tT0ken"
USER_CONFIG: dict[str, Any] = {"access_token": USER_TOKEN}
BOT_CONFIG: dict[str, Any] = {"access_token": BOT_TOKEN, "identity": "bot"}
#: Every tier enabled, for tests about a tool rather than about the gate.
ALL_TIERS = ["read", "send", "write", "destructive"]


@dataclass
class Call:
    method: str
    url: str
    data: Any
    headers: dict[str, str]
    timeout: Any = None
    allow_redirects: Any = None

    @property
    def api_method(self) -> str:
        return self.url.removeprefix("https://slack.com/api/")

    @property
    def is_json(self) -> bool:
        return self.headers.get("Content-Type", "").startswith("application/json")

    @property
    def args(self) -> dict[str, Any]:
        """The arguments sent, decoded from JSON or the form."""
        if self.is_json:
            return json.loads(self.data)
        return dict(self.data or {})


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

    @property
    def methods(self) -> list[str]:
        return [c.api_method for c in self.calls]


def response(
    status: int = 200,
    body: Any = None,
    *,
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> requests.Response:
    """A real ``requests.Response`` with a JSON ``body`` or raw ``content``."""
    r = requests.Response()
    r.status_code = status
    try:
        r.reason = http.HTTPStatus(status).phrase
    except ValueError:
        r.reason = ""
    if content is None:
        content = b"" if body is None else json.dumps(body).encode()
    r._content = content
    all_headers = dict(headers or {})
    if body is not None:
        all_headers.setdefault("Content-Type", "application/json; charset=utf-8")
    r.headers = CaseInsensitiveDict(all_headers)
    r.encoding = "utf-8"
    r.url = "https://slack.com/api/"
    return r


def ok(**fields: Any) -> requests.Response:
    """A Slack success answer."""
    return response(200, {"ok": True, **fields})


def slack_error(error: str, status: int = 200, **fields: Any) -> requests.Response:
    """A Slack ``ok: false`` answer."""
    return response(status, {"ok": False, "error": error, **fields})


def channels_page(*channels: dict[str, Any], cursor: str = "") -> requests.Response:
    return ok(channels=list(channels), response_metadata={"next_cursor": cursor})


def form(**fields: Any) -> dict[str, str]:
    """What the client form-encodes for ``fields``."""
    out: dict[str, str] = {}
    for key, value in fields.items():
        if isinstance(value, bool):
            out[key] = "true" if value else "false"
        elif isinstance(value, (list, dict)):
            out[key] = json.dumps(value)
        else:
            out[key] = str(value)
    return out


def query(url: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


@dataclass
class Harness:
    """A toolkit wired to a FakeSession with a recording, non-sleeping sleep."""

    session: FakeSession
    toolkit: SlackToolkit
    sleeps: list[float]

    def call(
        self,
        slug: str,
        arguments: dict[str, Any] | None = None,
        config: dict[str, Any] | None = None,
        **config_overrides: Any,
    ):
        base = dict(USER_CONFIG if config is None else config)
        base.update(config_overrides)
        return self.toolkit.call_tool(slug, arguments or {}, base)


def harness(*responses: Any, **toolkit_kwargs: Any) -> Harness:
    session = FakeSession(list(responses))
    sleeps: list[float] = []
    toolkit = SlackToolkit(session=session, sleep=sleeps.append, **toolkit_kwargs)  # type: ignore[arg-type]
    return Harness(session, toolkit, sleeps)
