"""A small read-only client for the Cloud Storage JSON API.

Endpoints (cloud.google.com/storage/docs/json_api):

* ``GET /storage/v1/b/{bucket}/o?prefix&delimiter&maxResults&pageToken`` ->
  ``{"items": [Object], "prefixes": [str], "nextPageToken"}``.
* ``GET /storage/v1/b/{bucket}/o/{object}?generation`` -> ``Object``.
* ``GET /storage/v1/b/{bucket}/o/{object}?alt=media&generation`` -> the bytes.

Every request goes to ``https://storage.googleapis.com`` (a fixed host) for
the configured bucket only, with the key's bearer token. Redirects are never
followed. Downloads are streamed and stop at ``max_bytes``. Error messages
never carry the token or key.
"""

from __future__ import annotations

from typing import Any, Self
from urllib.parse import quote

import requests

from aisquare_pipe_gcs.credentials import GcsCredentials, GcsTokenError, access_token

API_ROOT = "https://storage.googleapis.com/storage/v1"
DEFAULT_TIMEOUT = 30.0
#: The fields every object listing and read asks for (the version markers
#: included: generation, md5Hash, updated, etag).
OBJECT_FIELDS = (
    "name,bucket,size,contentType,generation,metageneration,md5Hash,crc32c,etag,"
    "updated,timeCreated,storageClass"
)
_MAX_DETAIL = 300
_CHUNK = 64 * 1024


class GcsAPIError(Exception):
    """A failed GCS call. ``status`` is the HTTP status (None when nothing came
    back); ``retry_after`` is set for a 429/503 that said when to retry."""

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


class ObjectTooLarge(GcsAPIError):
    """An object is bigger than the caller's ``max_bytes``."""


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if 0 <= seconds <= 3600 else None


def object_path(name: str) -> str:
    """An object name as one URL path segment (``/`` encoded too)."""
    return quote(name, safe="")


class GcsClient:
    """One credential's view of its bucket. Use it as a context manager: a
    session it opened itself is closed on exit; a passed-in ``session`` (tests,
    or a host that pools connections) is left open."""

    def __init__(
        self,
        credentials: GcsCredentials,
        *,
        session: Any = None,
        timeout: float = DEFAULT_TIMEOUT,
        budget: float | None = None,
    ) -> None:
        self._creds = credentials
        self._own_session = session is None
        self._session = session if session is not None else requests.Session()
        self._timeout = timeout if budget is None else max(0.1, min(timeout, budget))
        self._token: str | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        if self._own_session:
            self._session.close()

    @property
    def _bucket_url(self) -> str:
        return f"{API_ROOT}/b/{object_path(self._creds.bucket)}/o"

    # -- endpoints -----------------------------------------------------------

    def list_objects(
        self,
        *,
        prefix: str,
        page_size: int,
        page_token: str | None = None,
        delimiter: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "prefix": prefix,
            "maxResults": page_size,
            "fields": f"items({OBJECT_FIELDS}),prefixes,nextPageToken",
        }
        if page_token:
            params["pageToken"] = page_token
        if delimiter:
            params["delimiter"] = "/"
        return self._json(self._bucket_url, params)

    def get_metadata(
        self, name: str, *, generation: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"fields": OBJECT_FIELDS}
        if generation:
            params["generation"] = generation
        return self._json(f"{self._bucket_url}/{object_path(name)}", params)

    def download(self, name: str, *, generation: str | None, max_bytes: int) -> bytes:
        """The object's bytes at ``generation`` (so the bytes match the
        metadata read just before), at most ``max_bytes``."""
        params: dict[str, Any] = {"alt": "media"}
        if generation:
            params["generation"] = generation
        resp = self._get(f"{self._bucket_url}/{object_path(name)}", params, stream=True)
        try:
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_content(_CHUNK):
                total += len(chunk)
                if total > max_bytes:
                    raise ObjectTooLarge(
                        f"{name} is larger than {max_bytes} bytes",
                        status=resp.status_code,
                    )
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            resp.close()

    # -- transport -----------------------------------------------------------

    def _auth(self) -> dict[str, str]:
        if self._token is None:
            try:
                self._token = access_token(
                    self._creds, self._session, timeout=self._timeout
                )
            except GcsTokenError as e:
                raise GcsAPIError(str(e), status=401) from None
        return {"Authorization": f"Bearer {self._token}"}

    def _get(self, url: str, params: dict[str, Any], *, stream: bool = False) -> Any:
        try:
            resp = self._session.request(
                "GET",
                url,
                params=params,
                headers={"Accept": "application/json", **self._auth()},
                timeout=self._timeout,
                allow_redirects=False,
                stream=stream,
            )
        except requests.Timeout as e:
            raise GcsAPIError(
                "Cloud Storage did not answer in time", timed_out=True
            ) from e
        except requests.RequestException as e:
            raise GcsAPIError(
                f"could not reach Cloud Storage ({type(e).__name__})"
            ) from e
        status = resp.status_code
        if 300 <= status < 400 and status != 304:
            resp.close()
            raise GcsAPIError(
                f"Cloud Storage answered a redirect ({status}); redirects are not followed",
                status=status,
            )
        if status >= 400:
            detail = self._detail(resp)
            resp.close()
            raise GcsAPIError(
                f"Cloud Storage failed ({status})" + detail,
                status=status,
                retry_after=_retry_after(resp.headers.get("Retry-After"))
                if status in (429, 503)
                else None,
            )
        return resp

    def _json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        resp = self._get(url, params)
        try:
            data = resp.json()
        except ValueError as e:
            raise GcsAPIError(
                "Cloud Storage answered with a body that is not JSON",
                status=resp.status_code,
            ) from e
        if not isinstance(data, dict):
            raise GcsAPIError(
                "Cloud Storage answered with an unexpected body",
                status=resp.status_code,
            )
        return data

    def _detail(self, resp: Any) -> str:
        try:
            data = resp.json()
        except ValueError:
            data = None
        text = ""
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                text = error["message"]
            elif isinstance(error, str):
                text = error
        if self._token:
            text = text.replace(self._token, "[redacted]")
        text = " ".join(text.split())[:_MAX_DETAIL]
        return f": {text}" if text else ""


def object_record(item: Any) -> dict[str, Any]:
    """An Object resource as the tools and the source report it: the version
    markers are ``generation``, ``md5_hash`` and ``updated`` (``etag`` and
    ``crc32c`` too)."""
    if not isinstance(item, dict):
        return {}
    size = item.get("size")
    try:
        size_value: int | None = int(size) if size is not None else None
    except (TypeError, ValueError):
        size_value = None
    return {
        "name": item.get("name"),
        "size": size_value,
        "content_type": item.get("contentType"),
        "generation": item.get("generation"),
        "metageneration": item.get("metageneration"),
        "md5_hash": item.get("md5Hash"),
        "crc32c": item.get("crc32c"),
        "etag": item.get("etag"),
        "updated": item.get("updated"),
        "time_created": item.get("timeCreated"),
        "storage_class": item.get("storageClass"),
    }


__all__ = [
    "API_ROOT",
    "GcsAPIError",
    "GcsClient",
    "ObjectTooLarge",
    "object_path",
    "object_record",
]
