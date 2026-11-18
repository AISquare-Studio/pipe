"""Hermetic Glean fixtures: a scripted ``requests.Session`` stand-in and
builders for Glean's search results and documents. No socket is opened."""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from typing import Any

import requests
from requests.structures import CaseInsensitiveDict

HOST = "acme-be.glean.com"
BASE = f"https://{HOST}/rest/api/v1/"
TOKEN = "glean_s3cr3t_T0KEN_value_42"
USER_BLOB: dict[str, Any] = {"host": HOST, "token": TOKEN, "token_kind": "user"}
GLOBAL_BLOB: dict[str, Any] = {
    "host": HOST,
    "token": TOKEN,
    "token_kind": "global",
    "act_as": "reader@acme.com",
}


@dataclass
class Call:
    method: str
    url: str
    json: Any
    headers: dict[str, str]
    timeout: Any
    allow_redirects: Any

    @property
    def endpoint(self) -> str:
        return self.url.removeprefix(BASE)


def response(
    status: int = 200, body: Any = None, headers: dict[str, str] | None = None
) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status
    resp.headers = CaseInsensitiveDict(headers or {})
    raw = (
        body
        if isinstance(body, (bytes, str))
        else json.dumps(body if body is not None else {})
    )
    resp.raw = io.BytesIO(raw.encode() if isinstance(raw, str) else raw)
    resp.encoding = "utf-8"
    return resp


@dataclass
class FakeSession:
    responses: list[Any] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)

    def queue(self, *items: Any) -> FakeSession:
        self.responses.extend(items)
        return self

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append(
            Call(
                method=method,
                url=url,
                json=kwargs.get("json"),
                headers=dict(kwargs.get("headers") or {}),
                timeout=kwargs.get("timeout"),
                allow_redirects=kwargs.get("allow_redirects", True),
            )
        )
        if not self.responses:
            raise AssertionError(f"unexpected request {method} {url}")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        pass


def search_result(
    doc_id: str, *, title: str = "", updated: str = "2026-11-01T10:00:00Z"
) -> dict:
    return {
        "title": title or f"Doc {doc_id}",
        "url": f"https://docs.acme.com/{doc_id}",
        "snippets": [{"snippet": f"snippet of {doc_id}", "mimeType": "text/plain"}],
        "document": {
            "id": doc_id,
            "datasource": "confluence",
            "docType": "Page",
            "title": title or f"Doc {doc_id}",
            "url": f"https://docs.acme.com/{doc_id}",
            "metadata": {
                "datasource": "confluence",
                "documentId": doc_id,
                "updateTime": updated,
                "createTime": "2026-01-01T00:00:00Z",
                "author": {"name": "Ann Author"},
            },
        },
    }


def search_page(*ids: str, cursor: str | None = None) -> dict:
    body: dict[str, Any] = {
        "results": [search_result(i) for i in ids],
        "trackingToken": "t",
    }
    if cursor:
        body.update(cursor=cursor, hasMoreResults=True)
    else:
        body["hasMoreResults"] = False
    return body


def document(
    doc_id: str,
    *,
    text: list[str] | None = None,
    updated: str | None = "2026-11-01T10:00:00Z",
) -> dict:
    """A ``getdocuments`` document. ``updated=None`` leaves out
    ``metadata.updateTime``, as Glean may."""
    doc: dict[str, Any] = {
        "id": doc_id,
        "datasource": "confluence",
        "docType": "Page",
        "title": f"Doc {doc_id}",
        "url": f"https://docs.acme.com/{doc_id}",
        "content": {
            "fullTextList": text
            if text is not None
            else [f"line one of {doc_id}", "line two"]
        },
        "metadata": {
            "documentId": doc_id,
            "datasource": "confluence",
            "mimeType": "text/html",
            "updateTime": updated,
            "createTime": "2026-01-01T00:00:00Z",
            "author": {"name": "Ann Author"},
        },
    }
    if updated is None:
        del doc["metadata"]["updateTime"]
    return doc


def documents(*docs: dict, errors: dict[str, str] | None = None) -> dict:
    body = {d["id"]: d for d in docs}
    body.update({k: {"error": v} for k, v in (errors or {}).items()})
    return {"documents": body}
