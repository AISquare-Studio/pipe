"""A small read-only client for the Cloud Storage JSON API.

Endpoints (cloud.google.com/storage/docs/json_api):

* ``GET /storage/v1/b/{bucket}/o?prefix&delimiter&maxResults&pageToken&startOffset``
  -> ``{"items": [Object], "prefixes": [str], "nextPageToken"}`` (names in
  lexicographic order; ``startOffset`` is the first name to include).
* ``GET /storage/v1/b/{bucket}/o/{object}?generation`` -> ``Object``.
* ``GET /storage/v1/b/{bucket}/o/{object}?alt=media&generation`` -> the bytes.

Every request goes to ``https://storage.googleapis.com`` (a fixed host) for
the configured bucket only, with the key's bearer token. Redirects are never
followed. Downloads are streamed and stop at ``max_bytes``. Every failure,
the token exchange's and a download that breaks off midway included, is a
:class:`GcsAPIError` (``timed_out`` for a timeout). A 401 drops the token
from the cache, and a call whose cached token was rejected retries once with
a new one. Error messages never carry the token or key.
"""

from __future__ import annotations

from typing import Any, Self
from urllib.parse import quote

import requests
import urllib3.exceptions

from aisquare_pipe_gcs._http import retry_after_seconds, timed_out
from aisquare_pipe_gcs.credentials import (
    TOKEN_RATE_LIMITED,
    TOKEN_REFUSED,
    TOKEN_TIMED_OUT,
    GcsCredentials,
    GcsTokenError,
    forget_token,
    obtain_token,
)

API_ROOT = "https://storage.googleapis.com/storage/v1"
DEFAULT_TIMEOUT = 30.0
#: The fields every object listing and read asks for (the version markers
#: included: generation, md5Hash, updated, etag). size, md5Hash and crc32c
#: describe the STORED bytes; contentEncoding says when those are gzip.
OBJECT_FIELDS = (
    "name,bucket,size,contentType,contentEncoding,generation,metageneration,md5Hash,"
    "crc32c,etag,updated,timeCreated,storageClass"
)
#: Names no object can have (GCS refuses them), and that a URL path would
#: dot-segment-collapse into another endpoint.
INVALID_NAMES = frozenset({".", ".."})
_MAX_DETAIL = 300
_CHUNK = 64 * 1024


class GcsAPIError(Exception):
    """A failed GCS call. ``status`` is the HTTP status (None when nothing came
    back); ``retry_after`` is set for a 429/503 that said when to retry;
    ``timed_out`` for a timeout; ``key_refused`` when Google's token endpoint
    refused the service account key itself."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
        timed_out: bool = False,
        key_refused: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.retry_after = retry_after
        self.timed_out = timed_out
        self.key_refused = key_refused


class ObjectTooLarge(GcsAPIError):
    """An object is bigger than the caller's ``max_bytes``."""


def object_path(name: str) -> str:
    """An object name as one URL path segment (``/`` encoded too). ``.`` and
    ``..`` are refused: no object has them, and urllib3 would collapse them
    into the bucket's own URL."""
    if name in INVALID_NAMES:
        raise GcsAPIError(f"{name!r} is not an object name")
    return quote(name, safe="")


def _transport_error(e: BaseException, what: str) -> GcsAPIError:
    if timed_out(e):
        return GcsAPIError(
            f"Cloud Storage did not answer in time ({what})", timed_out=True
        )
    return GcsAPIError(f"could not {what} ({type(e).__name__})")


def _token_failure(e: GcsTokenError) -> GcsAPIError:
    """A token the endpoint didn't issue, as the GCS failure it is: only a
    refused key is 401 (unauthorized); a timeout stays a timeout, and an
    unreachable or failing endpoint is an upstream error a host may retry."""
    if e.kind == TOKEN_REFUSED:
        return GcsAPIError(e.message, status=401, key_refused=True)
    if e.kind == TOKEN_TIMED_OUT:
        return GcsAPIError(e.message, timed_out=True)
    if e.kind == TOKEN_RATE_LIMITED:
        return GcsAPIError(e.message, status=429, retry_after=e.retry_after)
    return GcsAPIError(e.message, retry_after=e.retry_after)


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
        self._token_from_cache = False
        #: Every token this client sent: redacted from any error text.
        self._sent_tokens: set[str] = set()

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
        start_offset: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "prefix": prefix,
            "maxResults": page_size,
            "fields": f"items({OBJECT_FIELDS}),prefixes,nextPageToken",
        }
        if page_token:
            params["pageToken"] = page_token
        if start_offset:
            params["startOffset"] = start_offset
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
        except (requests.RequestException, urllib3.exceptions.HTTPError, OSError) as e:
            # The body broke off midway: a read timeout (requests wraps it in
            # ConnectionError), a reset (ChunkedEncodingError), bad gzip...
            raise _transport_error(e, "read the object from Cloud Storage") from e
        finally:
            resp.close()

    # -- transport -----------------------------------------------------------

    def _auth(self) -> dict[str, str]:
        if self._token is None:
            try:
                self._token, self._token_from_cache = obtain_token(
                    self._creds, self._session, timeout=self._timeout
                )
            except GcsTokenError as e:
                raise _token_failure(e) from None
            self._sent_tokens.add(self._token)
        return {"Authorization": f"Bearer {self._token}"}

    def _drop_token(self) -> None:
        forget_token(self._creds, self._token)
        self._token = None

    def _send(self, url: str, params: dict[str, Any], *, stream: bool) -> Any:
        try:
            return self._session.request(
                "GET",
                url,
                params=params,
                headers={"Accept": "application/json", **self._auth()},
                timeout=self._timeout,
                allow_redirects=False,
                stream=stream,
            )
        except requests.RequestException as e:
            raise _transport_error(e, "reach Cloud Storage") from e

    def _get(self, url: str, params: dict[str, Any], *, stream: bool = False) -> Any:
        resp = self._send(url, params, stream=stream)
        if resp.status_code == 401:
            # Google no longer takes this token (key deleted or disabled,
            # account disabled, token revoked): drop it so no later call
            # reuses it for the rest of its hour. A token from the cache gets
            # one retry with a new one; a new one that fails is final.
            from_cache = self._token_from_cache
            self._drop_token()
            if from_cache:
                resp.close()
                resp = self._send(url, params, stream=stream)
                if resp.status_code == 401:
                    self._drop_token()
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
                retry_after=retry_after_seconds(resp.headers.get("Retry-After"))
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
        except (ValueError, requests.RequestException):
            data = None
        text = ""
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                text = error["message"]
            elif isinstance(error, str):
                text = error
        for token in self._sent_tokens:
            text = text.replace(token, "[redacted]")
        text = " ".join(text.split())[:_MAX_DETAIL]
        return f": {text}" if text else ""


def object_record(item: Any) -> dict[str, Any]:
    """An Object resource as the tools and the source report it: the version
    markers are ``generation``, ``md5_hash`` and ``updated`` (``etag`` and
    ``crc32c`` too). ``size``, ``md5_hash`` and ``crc32c`` describe the stored
    bytes: for ``content_encoding`` gzip those are compressed, while a read
    returns them inflated."""
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
        "content_encoding": item.get("contentEncoding"),
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
    "INVALID_NAMES",
    "GcsAPIError",
    "GcsClient",
    "ObjectTooLarge",
    "object_path",
    "object_record",
]
