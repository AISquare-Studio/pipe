"""Google Drive v3 REST client for the Google Drive toolkit, source and sink.

This module is the package's only HTTP boundary (tests mock the
``requests.Session`` handed to it). One :class:`GoogleDriveRestClient` serves
one tool call, one pull or one push, with one access token. The token rides
the ``Authorization`` header only: it never appears in a URL, a log line, an
exception message or a repr.

Non-2xx answers raise :class:`GoogleDriveAPIError` carrying the status, Google's
own message and its first error ``reason``; callers turn that into a failed
``ToolResult`` / ``PushResult`` / ``PipelineError``.

Rate limits: a 429, or a 403 whose reason is ``rateLimitExceeded`` /
``userRateLimitExceeded``, is retried up to ``max_retries`` times with
exponential backoff (1s, 2s, 4s, ... — or ``Retry-After`` when Google sends
one), as Google's Drive guidance asks. A 5xx on a GET is retried the same way.
The total sleep per call is capped at ``max_retry_wait`` seconds; a longer
wait fails at once (with ``GoogleDriveAPIError.retry_after`` set). Other 403s
(``dailyLimitExceeded``, ``storageQuotaExceeded``, permission errors) are not
retried.

Time budget: with ``budget`` (seconds; the host's ``config["timeout_seconds"]``)
the whole call — every attempt and every retry sleep — fits inside it. Each
request's timeout shrinks to what is left, a retry that would sleep past it
fails at once, and running out raises a ``timed_out`` error.

Uploads: :meth:`GoogleDriveRestClient.upload` sends up to
:data:`MULTIPART_MAX` bytes as one ``multipart/related`` request and anything
larger (or a stream of unknown size) through a resumable session in
:data:`CHUNK_SIZE` chunks. A chunk that fails with a 5xx or a dropped
connection is resumed: back off, ask Google how much it holds, continue.
"""

from __future__ import annotations

import io
import json as jsonlib
import logging
import math
import secrets
import time
import urllib.parse
from collections.abc import Callable, Mapping
from typing import IO, Any, Self

import requests

logger = logging.getLogger("aisquare.pipe.googledrive")

API_BASE = "https://www.googleapis.com/drive/v3"
UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3"
USER_AGENT = "aisquare-pipe-googledrive"

DEFAULT_TIMEOUT = 30.0
#: Per-request timeout for file bytes (download bodies, upload chunks).
MEDIA_TIMEOUT = 300.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_MAX_RETRY_WAIT = 60.0

#: Largest upload sent as one multipart request; bigger ones go resumable.
MULTIPART_MAX = 5 * 1024 * 1024
#: Resumable chunk size; Google requires a multiple of 256 KiB.
CHUNK_SIZE = 32 * 256 * 1024  # 8 MiB

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
GOOGLE_APPS_PREFIX = "application/vnd.google-apps."

#: 403/429 reasons that mean "slow down" (retried); every other 403 is final.
RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})
_RETRYABLE_SERVER_ERRORS = frozenset({500, 502, 503, 504})
_BACKOFF_BASE = 1.0
_MAX_MESSAGE = 500
#: Time a retry keeps for its request after a backoff sleep (budgeted calls).
_REQUEST_ROOM = 2.0
#: Read size for capped downloads.
_READ_CHUNK = 64 * 1024
#: Hosts an upload session URL may point at (it comes back in a header).
_UPLOAD_HOSTS = frozenset({"www.googleapis.com", "googleapis.com"})


class GoogleDriveAPIError(Exception):
    """A non-2xx Google answer (``status``) or a transport failure (``status=0``).

    ``message`` is Google's own error text; ``reason`` its first
    ``errors[].reason`` (``notFound``, ``insufficientFilePermissions``,
    ``userRateLimitExceeded``, ...); ``retry_after`` is set when the failure is
    a rate limit, in seconds; ``timed_out`` when Google did not answer in time
    (or the budget ran out).
    """

    def __init__(
        self,
        status: int,
        message: str,
        *,
        reason: str | None = None,
        retry_after: float | None = None,
        timed_out: bool = False,
    ) -> None:
        super().__init__(f"{status}: {message}" if status else message)
        self.status = status
        self.message = message
        self.reason = reason
        self.retry_after = retry_after
        self.timed_out = timed_out

    @property
    def rate_limited(self) -> bool:
        return self.retry_after is not None


class DownloadLimitError(GoogleDriveAPIError):
    """A download grew past the caller's byte cap (the rest was not read)."""

    def __init__(self, limit: int) -> None:
        super().__init__(0, f"the file is larger than the {limit:,}-byte download limit")
        self.limit = limit


class GoogleDriveRestClient:
    """Bearer-token client for the Drive v3 API (one per call)."""

    def __init__(
        self,
        token: str,
        *,
        session: requests.Session | None = None,
        base_url: str = API_BASE,
        upload_url: str = UPLOAD_BASE,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_retry_wait: float = DEFAULT_MAX_RETRY_WAIT,
        budget: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._token = token
        self._owns_session = session is None
        self._session = session if session is not None else requests.Session()
        self.base_url = base_url.rstrip("/")
        self.upload_url = upload_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.max_retry_wait = max(0.0, max_retry_wait)
        self._sleep = sleep
        self._monotonic = monotonic
        self.budget = budget if budget is not None and budget > 0 else None
        self._deadline = monotonic() + self.budget if self.budget is not None else None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(base_url={self.base_url!r})"

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def _remaining(self) -> float | None:
        """Seconds left in the budget (None: unbounded)."""
        if self._deadline is None:
            return None
        return self._deadline - self._monotonic()

    # ------------------------------------------------------------------ http

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        data: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        upload: bool = False,
        stream: bool = False,
        timeout: float | None = None,
    ) -> requests.Response:
        """Send one request (``path`` is relative to the API base, or to the
        upload base when ``upload``); return the 2xx response or raise
        :class:`GoogleDriveAPIError`."""
        base = self.upload_url if upload else self.base_url
        all_headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": USER_AGENT,
            **(headers or {}),
        }
        return self._send(
            method,
            base + path,
            params=params,
            json=json,
            data=data,
            headers=all_headers,
            stream=stream,
            timeout=timeout,
        )

    def get(self, path: str, params: Mapping[str, Any] | None = None, **kw: Any) -> requests.Response:
        return self.request("GET", path, params=params, **kw)

    def _send(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None,
        json: Any,
        data: bytes | None,
        headers: Mapping[str, str],
        stream: bool,
        timeout: float | None,
        allow_redirects: bool = True,
    ) -> requests.Response:
        per_request = self.timeout if timeout is None else timeout
        waited = 0.0
        attempt = 0
        while True:
            remaining = self._remaining()
            if remaining is not None and remaining <= 0:
                raise GoogleDriveAPIError(
                    0, f"Google Drive call ran out of its {self.budget:g}s time budget", timed_out=True
                )
            request_timeout = per_request if remaining is None else min(per_request, remaining)
            started = time.monotonic()
            try:
                response = self._session.request(
                    method,
                    url,
                    params=dict(params) if params else None,
                    json=json,
                    data=data,
                    headers=dict(headers),
                    timeout=request_timeout,
                    stream=stream,
                    allow_redirects=allow_redirects,
                )
            except requests.Timeout as e:  # before RequestException: ConnectTimeout is both
                raise GoogleDriveAPIError(
                    0,
                    f"Google Drive did not answer within {round(request_timeout, 1):g}s ({type(e).__name__})",
                    timed_out=True,
                ) from None
            except requests.RequestException as e:
                # str(e) can carry the URL (and so argument values): name the type only.
                raise GoogleDriveAPIError(
                    0, f"network error talking to Google Drive ({type(e).__name__})"
                ) from None
            logger.debug(
                "googledrive: %s -> %s in %.0fms",
                method,
                response.status_code,
                (time.monotonic() - started) * 1000,
            )
            if response.status_code < 400:
                return response

            limited = is_rate_limited(response)
            wait: float | None = None
            if limited or (method == "GET" and response.status_code in _RETRYABLE_SERVER_ERRORS):
                wait = retry_wait(response, attempt)
            remaining = self._remaining()
            fits = wait is not None and wait <= self.max_retry_wait - waited
            if fits and wait is not None and remaining is not None:
                # The retry itself needs time too: never sleep the budget away.
                fits = wait + min(per_request, _REQUEST_ROOM) <= remaining
            if wait is None or attempt >= self.max_retries or not fits:
                status = response.status_code
                message, reason = error_details(response)
                if stream:
                    response.close()
                raise GoogleDriveAPIError(
                    status, message, reason=reason, retry_after=wait if limited else None
                )
            attempt += 1
            logger.warning(
                "googledrive: HTTP %s, retrying in %.0fs (retry %d/%d)",
                response.status_code,
                wait,
                attempt,
                self.max_retries,
            )
            if stream:
                response.close()
            self._sleep(wait)
            waited += wait

    # ------------------------------------------------------------- downloads

    def download(
        self, file_id: str, *, export_mime: str | None = None, stream: bool = False
    ) -> requests.Response:
        """The file's bytes: ``alt=media`` for a stored file, or Google's
        ``/export`` in ``export_mime`` for a Google Docs/Sheets/Slides file."""
        quoted = quote_id(file_id)
        if export_mime:
            return self.request(
                "GET",
                f"/files/{quoted}/export",
                params={"mimeType": export_mime},
                headers={"Accept": "*/*"},
                stream=stream,
                timeout=MEDIA_TIMEOUT,
            )
        return self.request(
            "GET",
            f"/files/{quoted}",
            params={"alt": "media", "supportsAllDrives": "true"},
            headers={"Accept": "*/*"},
            stream=stream,
            timeout=MEDIA_TIMEOUT,
        )

    def download_bytes(
        self, file_id: str, *, export_mime: str | None = None, max_bytes: int
    ) -> bytes:
        """:meth:`download`, read in chunks: more than ``max_bytes`` raises
        :class:`DownloadLimitError`, and a spent time budget raises a
        ``timed_out`` error between chunks (a request timeout bounds each
        read, not the whole body)."""
        response = self.download(file_id, export_mime=export_mime, stream=True)
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in response.iter_content(_READ_CHUNK):
                total += len(chunk)
                if total > max_bytes:
                    raise DownloadLimitError(max_bytes)
                remaining = self._remaining()
                if remaining is not None and remaining <= 0:
                    raise GoogleDriveAPIError(
                        0,
                        f"Google Drive download ran out of its {self.budget:g}s time budget",
                        timed_out=True,
                    )
                chunks.append(chunk)
        except requests.Timeout as e:  # before RequestException: ConnectTimeout is both
            raise GoogleDriveAPIError(
                0, f"Google Drive stopped sending the file ({type(e).__name__})", timed_out=True
            ) from None
        except requests.RequestException as e:
            raise GoogleDriveAPIError(
                0, f"network error downloading from Google Drive ({type(e).__name__})"
            ) from None
        finally:
            response.close()
        return b"".join(chunks)

    # --------------------------------------------------------------- uploads

    def upload(
        self,
        content: bytes | IO[bytes],
        *,
        metadata: Mapping[str, Any],
        content_type: str,
        size: int | None = None,
        file_id: str | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a file (``file_id=None``, POST) or replace one's content
        (PATCH) with ``metadata`` + ``content``; return Google's file resource.

        ``content`` up to :data:`MULTIPART_MAX` bytes goes as one multipart
        request; larger content, or a stream whose ``size`` is unknown, goes
        through a resumable session.
        """
        method = "PATCH" if file_id else "POST"
        path = f"/files/{quote_id(file_id)}" if file_id else "/files"
        query = {"supportsAllDrives": "true", **(params or {})}
        if isinstance(content, (bytes, bytearray)):
            size = len(content)
            if size <= MULTIPART_MAX:
                return self._upload_multipart(method, path, bytes(content), metadata, content_type, query)
            stream: IO[bytes] = io.BytesIO(content)
        else:
            stream = content
            if size is not None and size <= MULTIPART_MAX:
                return self._upload_multipart(
                    method, path, stream.read(), metadata, content_type, query
                )
        return self._upload_resumable(method, path, stream, size, metadata, content_type, query)

    def _upload_multipart(
        self,
        method: str,
        path: str,
        content: bytes,
        metadata: Mapping[str, Any],
        content_type: str,
        query: Mapping[str, Any],
    ) -> dict[str, Any]:
        boundary = f"aisquare-pipe-{secrets.token_hex(12)}"
        body, header = multipart_body(metadata, content, content_type, boundary)
        response = self.request(
            method,
            path,
            params={**query, "uploadType": "multipart"},
            data=body,
            headers={"Content-Type": header},
            upload=True,
            timeout=MEDIA_TIMEOUT,
        )
        return _object(response)

    def _upload_resumable(
        self,
        method: str,
        path: str,
        stream: IO[bytes],
        size: int | None,
        metadata: Mapping[str, Any],
        content_type: str,
        query: Mapping[str, Any],
    ) -> dict[str, Any]:
        headers = {"X-Upload-Content-Type": content_type}
        if size is not None:
            headers["X-Upload-Content-Length"] = str(size)
        started = self.request(
            method,
            path,
            params={**query, "uploadType": "resumable"},
            json=dict(metadata),
            headers=headers,
            upload=True,
        )
        session_url = started.headers.get("Location", "")
        _check_upload_session(session_url)
        offset = 0
        chunk = _read_full(stream, CHUNK_SIZE)
        while True:
            ahead = _read_full(stream, CHUNK_SIZE) if len(chunk) == CHUNK_SIZE else b""
            last = not ahead
            total = str(offset + len(chunk)) if last else (str(size) if size is not None else "*")
            stalls = 0
            failures = 0
            while True:
                resumed = False
                try:
                    response = self._put_chunk(session_url, chunk, offset, total)
                except GoogleDriveAPIError as e:
                    failures += 1
                    response, failures = self._resume(session_url, total, e, failures)
                    resumed = True
                if response.status_code in (200, 201):
                    return _object(response)
                # 308 Resume Incomplete: Range says how many bytes Google holds.
                received = _received_bytes(response)
                if not offset <= received <= offset + len(chunk):
                    raise GoogleDriveAPIError(
                        0, "Google Drive upload session lost track of the upload; retry the upload"
                    )
                if received == offset and not resumed:
                    stalls += 1
                    if stalls > self.max_retries:
                        raise GoogleDriveAPIError(
                            0, "Google Drive upload session stopped accepting data; retry the upload"
                        )
                chunk = chunk[received - offset :]
                offset = received
                if not chunk:
                    break
            if last:
                raise GoogleDriveAPIError(
                    0, "Google Drive upload session ended without returning the file"
                )
            chunk = ahead

    def _resume(
        self, session_url: str, total: str, error: GoogleDriveAPIError, failures: int
    ) -> tuple[requests.Response, int]:
        """After a chunk PUT failed with a 5xx or a dropped connection: back
        off, then ask Google how much of the upload it holds (an empty PUT with
        ``Content-Range: bytes */<total>``) so the upload carries on from
        there. Bounded by ``max_retries`` per chunk and by the time budget;
        returns the status answer and the failure count so far."""
        while True:
            if not _resumable(error) or failures > self.max_retries:
                raise error
            wait = _BACKOFF_BASE * (2 ** (failures - 1))
            remaining = self._remaining()
            if remaining is not None and wait + min(MEDIA_TIMEOUT, _REQUEST_ROOM) > remaining:
                raise error
            logger.warning(
                "googledrive: upload chunk failed (HTTP %s), resuming in %.0fs (retry %d/%d)",
                error.status,
                wait,
                failures,
                self.max_retries,
            )
            self._sleep(wait)
            try:
                return self._put_chunk(session_url, b"", 0, total), failures
            except GoogleDriveAPIError as e:
                error = e
                failures += 1

    def _put_chunk(self, session_url: str, chunk: bytes, offset: int, total: str) -> requests.Response:
        if chunk:
            content_range = f"bytes {offset}-{offset + len(chunk) - 1}/{total}"
        else:
            content_range = f"bytes */{total}"
        # The session URL is its own credential: the bearer token is not re-sent.
        return self._send(
            "PUT",
            session_url,
            params=None,
            json=None,
            data=chunk,
            headers={"Content-Range": content_range, "User-Agent": USER_AGENT},
            stream=False,
            timeout=MEDIA_TIMEOUT,
            allow_redirects=False,  # 308 here means "resume incomplete", not a redirect
        )


# --------------------------------------------------------------------------
# Request / response helpers
# --------------------------------------------------------------------------


def quote_id(value: str) -> str:
    """A Drive id (file, folder, permission, comment, shared drive) as one
    URL path segment. Ids are ``[A-Za-z0-9_-]``; anything else is encoded, and
    a ``.``/``..`` id — which the HTTP stack would resolve as a dot segment
    and so reroute the call — is refused."""
    text = str(value).strip()
    if text in ("", ".", ".."):
        raise ValueError("a Google Drive id must not be empty, '.' or '..'")
    return urllib.parse.quote(text, safe="")


def multipart_body(
    metadata: Mapping[str, Any], content: bytes, content_type: str, boundary: str
) -> tuple[bytes, str]:
    """A ``multipart/related`` upload body (JSON metadata part + media part)
    and its ``Content-Type`` header."""
    head = (
        f"--{boundary}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{jsonlib.dumps(dict(metadata))}\r\n"
        f"--{boundary}\r\n"
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    return head + content + tail, f"multipart/related; boundary={boundary}"


def _read_full(stream: IO[bytes], size: int) -> bytes:
    """Up to ``size`` bytes, looping over short reads (pipes, sockets)."""
    parts: list[bytes] = []
    remaining = size
    while remaining > 0:
        block = stream.read(remaining)
        if not block:
            break
        parts.append(block)
        remaining -= len(block)
    return b"".join(parts)


def _received_bytes(response: requests.Response) -> int:
    """Bytes Google holds after a 308, from ``Range: bytes=0-N`` (none: 0)."""
    value = response.headers.get("Range", "")
    _, _, end = value.partition("-")
    try:
        return int(end) + 1
    except ValueError:
        return 0


def _resumable(error: GoogleDriveAPIError) -> bool:
    """A chunk failure worth resuming after: a 5xx, or a transport failure
    (connection reset, read timeout). Rate limits were already retried."""
    return error.retry_after is None and (
        error.status == 0 or error.status in _RETRYABLE_SERVER_ERRORS
    )


def _check_upload_session(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or (parts.hostname or "").lower() not in _UPLOAD_HOSTS:
        raise GoogleDriveAPIError(0, "Google Drive did not return a usable upload session URL")


def _object(response: requests.Response) -> dict[str, Any]:
    body = json_body(response)
    return body if isinstance(body, dict) else {}


def json_body(response: requests.Response) -> Any:
    """Parsed JSON body; ``None`` for an empty body (e.g. 204)."""
    if response.status_code == 204 or not response.content:
        return None
    try:
        return response.json()
    except Exception:  # noqa: BLE001 — ValueError, or RecursionError on absurd nesting
        raise GoogleDriveAPIError(
            response.status_code, "Google Drive returned a response that is not JSON"
        ) from None


def _error_payload(response: requests.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 — any unparseable body (RecursionError included) has no details
        return {}
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, dict) else {}


def error_details(response: requests.Response) -> tuple[str, str | None]:
    """Google's error ``message`` and first ``errors[].reason``."""
    error = _error_payload(response)
    message = str(error.get("message") or "")
    reason: str | None = None
    errors = error.get("errors")
    if isinstance(errors, list):
        for item in errors:
            if isinstance(item, dict) and item.get("reason"):
                reason = str(item["reason"])
                message = message or str(item.get("message") or "")
                break
    message = message or response.reason or "HTTP error"
    return message[:_MAX_MESSAGE], reason


def is_rate_limited(response: requests.Response) -> bool:
    """A 429, or a 403 whose reason is a rate limit (not a daily/storage quota)."""
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    _, reason = error_details(response)
    return reason in RATE_LIMIT_REASONS


def retry_wait(response: requests.Response, attempt: int) -> float:
    """Seconds to wait before retry ``attempt + 1``: ``Retry-After`` when
    Google sends a finite one, else exponential backoff (1s, 2s, 4s, ...)."""
    header = response.headers.get("Retry-After")
    if header:
        try:
            seconds = float(header)
        except ValueError:
            seconds = math.nan
        if math.isfinite(seconds):  # inf / 1e309 / nan would overflow callers' arithmetic
            return max(0.0, seconds)
    return _BACKOFF_BASE * (2**attempt)


def escape_query(value: str) -> str:
    """A value for a single-quoted string in a Drive ``q`` query."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def group_query(clause: str) -> str:
    """A caller's Drive query clause as one parenthesised group, safe to AND
    with a scope (``'<folder>' in parents``). Refused (ValueError) when its
    parentheses outside quoted strings do not balance, close the group early,
    or a quoted string is left open — any of which would let the clause
    escape the group and the scope with it."""
    depth = 0
    quoted = False
    escaped = False
    for ch in clause:
        if quoted:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == "'":
                quoted = False
        elif ch == "'":
            quoted = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("query closes a parenthesis it never opened")
    if quoted:
        raise ValueError("query leaves a quoted string open")
    if depth:
        raise ValueError("query leaves a parenthesis open")
    return f"({clause})"
