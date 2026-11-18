"""A small client for Glean's Client API (read-only: search and read documents).

Endpoints (developers.glean.com, Client API):

* ``POST /rest/api/v1/search``: ``{"query", "pageSize", "cursor",
  "requestOptions"}`` -> ``{"results", "cursor", "hasMoreResults"}``.
* ``POST /rest/api/v1/getdocuments``: ``{"documentSpecs": [{"id"} | {"url"}],
  "includeFields": ["DOCUMENT_CONTENT"]}`` -> ``{"documents": {key: Document |
  {"error": str}}}``.

Every request goes to ``https://<host>/rest/api/v1/`` with the credential's
headers (:meth:`GleanCredentials.headers`). Redirects are never followed, so
the token is only ever sent to the checked host. Error messages never carry
the token.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Self

import requests

from aisquare_pipe_glean.credentials import GleanCredentials

DEFAULT_TIMEOUT = 30.0
#: Glean requires ``facetBucketSize`` whenever ``requestOptions`` is sent.
_FACET_BUCKET_SIZE = 10
#: Longest provider error text kept in a message.
_MAX_DETAIL = 300


class GleanAPIError(Exception):
    """A failed Glean call. ``status`` is the HTTP status (None when nothing
    came back); ``retry_after`` is set for a 429 that said when to retry."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
        timed_out: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.retry_after = retry_after
        self.timed_out = timed_out


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if 0 <= seconds <= 3600 else None


class GleanClient:
    """One credential's view of Glean. Use it as a context manager: a
    session it opened itself is closed on exit; a passed-in ``session`` (tests,
    or a host that pools connections) is left open."""

    def __init__(
        self,
        credentials: GleanCredentials,
        *,
        session: Any = None,
        timeout: float = DEFAULT_TIMEOUT,
        budget: float | None = None,
    ) -> None:
        self._creds = credentials
        self._own_session = session is None
        self._session = session if session is not None else requests.Session()
        self._timeout = timeout if budget is None else max(0.1, min(timeout, budget))

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        if self._own_session:
            self._session.close()

    # -- endpoints -----------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        page_size: int,
        cursor: str | None = None,
        datasources: Sequence[str] = (),
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"query": query, "pageSize": page_size}
        if cursor:
            body["cursor"] = cursor
        if datasources:
            body["requestOptions"] = {
                "facetBucketSize": _FACET_BUCKET_SIZE,
                "datasourcesFilter": list(datasources),
            }
        return self._post("search", body)

    def get_documents(
        self,
        *,
        ids: Iterable[str] = (),
        urls: Iterable[str] = (),
        include_content: bool = True,
    ) -> dict[str, Any]:
        specs = [{"id": i} for i in ids] + [{"url": u} for u in urls]
        body: dict[str, Any] = {"documentSpecs": specs}
        if include_content:
            body["includeFields"] = ["DOCUMENT_CONTENT"]
        return self._post("getdocuments", body)

    # -- transport -----------------------------------------------------------

    def _redact(self, text: str) -> str:
        return (
            text.replace(self._creds.token, "[redacted]") if self._creds.token else text
        )

    def _post(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            **self._creds.headers(),
        }
        try:
            resp = self._session.request(
                "POST",
                self._creds.base_url + endpoint,
                json=body,
                headers=headers,
                timeout=self._timeout,
                allow_redirects=False,
            )
        except requests.Timeout as e:
            raise GleanAPIError(
                f"Glean {endpoint} did not answer in time", timed_out=True
            ) from e
        except requests.RequestException as e:
            raise GleanAPIError(f"could not reach Glean ({type(e).__name__})") from e
        status = resp.status_code
        if 300 <= status < 400:
            raise GleanAPIError(
                f"Glean {endpoint} answered a redirect ({status}); redirects are not followed",
                status=status,
            )
        if status >= 400:
            raise GleanAPIError(
                f"Glean {endpoint} failed ({status})" + self._detail(resp),
                status=status,
                retry_after=_retry_after(resp.headers.get("Retry-After"))
                if status == 429
                else None,
            )
        try:
            data = resp.json()
        except ValueError as e:
            raise GleanAPIError(
                f"Glean {endpoint} answered with a body that is not JSON", status=status
            ) from e
        if not isinstance(data, dict):
            raise GleanAPIError(
                f"Glean {endpoint} answered with an unexpected body", status=status
            )
        return data

    def _detail(self, resp: Any) -> str:
        try:
            data = resp.json()
        except ValueError:
            data = None
        text = ""
        if isinstance(data, dict):
            for key in ("message", "error", "errorMessage"):
                if isinstance(data.get(key), str):
                    text = data[key]
                    break
        elif isinstance(getattr(resp, "text", None), str):
            text = resp.text
        text = self._redact(" ".join(text.split()))[:_MAX_DETAIL]
        return f": {text}" if text else ""
