"""Slack Web API client for :class:`~aisquare_pipe_slack.toolkit.SlackToolkit`.

This module is the toolkit's only HTTP boundary (tests mock the
``requests.Session`` handed to it). One :class:`SlackWebClient` serves one tool
call with one token. The token rides the ``Authorization`` header only: it
never appears in a body, a URL, a log line, an exception message or a repr.

Every call is ``POST https://slack.com/api/<method>``. ``method`` always comes
from the toolkit's catalog, never from tool arguments. Arguments go
form-encoded by default; the write methods that take structured arguments
(``chat.postMessage`` and friends) go as JSON, which Slack recommends for them.

Unlike most REST APIs, Slack reports almost every failure as HTTP 200 with
``{"ok": false, "error": "<code>"}``; only rate limits are guaranteed to use
HTTP 429. :meth:`SlackWebClient.call` folds the HTTP status and the ``ok``
field onto one error type, :class:`SlackAPIError`, and never lets a parse
failure escape: an unreadable body keeps its HTTP status.

Rate limits (HTTP 429, or ``ok: false`` with ``ratelimited``) are retried up to
``max_retries`` times, sleeping for ``Retry-After`` (60 s when absent or
unusable: Slack's windows are per minute). Retrying a 429 is safe for writes
too: Slack did not process the request. Server errors (5xx,
``internal_error``, ``service_unavailable``…) are retried only for idempotent
(read) calls — retrying a ``chat.postMessage`` could post twice. The total
sleep per call is capped at ``max_retry_wait``.

Time budget: with ``budget`` (seconds; the host's ``config["timeout_seconds"]``)
the whole call — every attempt and every retry sleep — fits inside it. A
retry whose sleep would not leave ``min(timeout, 2s)`` for the request fails
at once (a rate limit as such, with ``retry_after``) rather than sleeping the
budget away and then reporting a timeout.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
import urllib.parse
from collections.abc import Callable, Mapping
from typing import Any, Self

import requests
import urllib3.exceptions
import urllib3.util

from aisquare_pipe_slack.constants import API_BASE, USER_AGENT

logger = logging.getLogger("aisquare.pipe.slack")

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_MAX_RETRY_WAIT = 60.0
#: Wait assumed for a rate limit without a usable ``Retry-After``.
DEFAULT_RETRY_AFTER = 60.0
#: Longest ``Retry-After`` taken at face value; larger is capped.
MAX_RETRY_AFTER = 3600.0

#: Backoff for a server error on an idempotent call (doubles per retry).
_SERVER_ERROR_WAIT = 1.0
#: Time a retried request needs after a sleep (see the module docstring).
_REQUEST_FLOOR = 2.0
#: ``chat.postMessage``, ``users.profile.get``, ``api.test``.
_METHOD = re.compile(r"^[a-z]+(\.[a-zA-Z]+)+$")

#: What a Slack error code looks like. Anything else in ``error`` (or
#: ``needed``) is not passed on: it reaches messages and ``ToolResult.data``,
#: and core masks secrets only in the message.
SAFE_CODE = re.compile(r"[a-z0-9_]+")
_SAFE_SCOPES = re.compile(r"[a-z0-9_.:,]+")

#: ``ok: false`` codes that are rate limits.
RATE_LIMIT_ERRORS = frozenset({"ratelimited", "rate_limited"})
#: ``ok: false`` codes Slack asks to retry shortly ("too many concurrent
#: requests", "canvas editing locked, retry after a short delay"): rate
#: limits too, with a short default wait instead of a minute.
RETRY_SOON_ERRORS = frozenset({"too_many_requests", "canvas_editing_locked"})
RETRY_SOON_WAIT = 2.0
#: ``ok: false`` codes that are Slack-side failures (retried for reads only).
SERVER_ERRORS = frozenset({"fatal_error", "internal_error", "service_unavailable"})


class SlackAPIError(Exception):
    """A failed Slack call.

    ``error`` is Slack's own code (``channel_not_found``) or, when Slack sent
    none, a synthetic one: ``http_<status>``, ``invalid_response``,
    ``network_error`` or ``timeout``. ``status`` is the HTTP status (0 when
    Slack never answered). ``retry_after`` (seconds) is set on rate limits;
    ``needed`` carries the scope(s) a ``missing_scope`` asks for.
    """

    def __init__(
        self,
        method: str,
        error: str,
        *,
        status: int = 0,
        retry_after: float | None = None,
        timed_out: bool = False,
        needed: str | None = None,
    ) -> None:
        super().__init__(f"{method}: {error}")
        self.method = method
        self.error = error
        self.status = status
        self.retry_after = retry_after
        self.timed_out = timed_out
        self.needed = needed


class SlackWebClient:
    """Bearer-token client for ``slack.com/api`` (one per tool call)."""

    def __init__(
        self,
        token: str,
        *,
        session: requests.Session | None = None,
        base_url: str = API_BASE,
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

    def _send(self, label: str, verb: str, url: str, **kwargs: Any) -> requests.Response:
        """One request outside the Web API (file bytes), inside the time
        budget, redirects never followed by requests itself. Transport
        failures raise :class:`SlackAPIError` labelled ``label``."""
        remaining = self._remaining()
        if remaining is not None and remaining <= 0:
            raise SlackAPIError(label, "timeout", timed_out=True)
        timeout = self.timeout if remaining is None else min(self.timeout, remaining)
        try:
            return self._session.request(verb, url, timeout=timeout, allow_redirects=False, **kwargs)
        except requests.Timeout:
            raise SlackAPIError(label, "timeout", timed_out=True) from None
        except requests.RequestException as e:
            logger.warning("slack: %s network error (%s)", label, type(e).__name__)
            raise SlackAPIError(label, "network_error") from None

    def download(self, url: str, *, max_bytes: int) -> tuple[bytes, str]:
        """A Slack-hosted file's bytes and content type (see :func:`_download`)."""
        return _download(self, url, max_bytes=max_bytes)

    def upload_external(self, upload_url: str, data: bytes) -> None:
        """POST bytes to a v2 upload URL (see :func:`_upload`)."""
        _upload(self, upload_url, data)

    def call(
        self,
        method: str,
        args: Mapping[str, Any] | None = None,
        *,
        json_body: bool = False,
        idempotent: bool = False,
        retry: bool = True,
        files: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call one Web API method; return Slack's payload (``ok`` is true) or
        raise :class:`SlackAPIError`. ``None`` arguments are dropped.
        ``idempotent`` calls (reads) are also retried on server errors;
        ``retry=False`` never retries. ``files`` (``{field: (name, bytes,
        mime)}``) sends a multipart form, as ``users.setPhoto`` needs."""
        if not _METHOD.match(method):
            raise ValueError(f"not a Slack method name: {method!r}")
        fields = {k: v for k, v in (args or {}).items() if v is not None}
        headers = {"Authorization": f"Bearer {self._token}", "User-Agent": USER_AGENT}
        if json_body:
            headers["Content-Type"] = "application/json; charset=utf-8"
            body: Any = json.dumps(fields).encode("utf-8")
        else:
            body = {k: form_value(v) for k, v in fields.items()}
        url = f"{self.base_url}/{method}"
        waited = 0.0
        attempt = 0
        while True:
            remaining = self._remaining()
            if remaining is not None and remaining <= 0:
                raise SlackAPIError(method, "timeout", timed_out=True)
            timeout = self.timeout if remaining is None else min(self.timeout, remaining)
            started = time.monotonic()
            try:
                response = self._session.request(
                    "POST",
                    url,
                    data=body,
                    files=dict(files) if files else None,
                    headers=headers,
                    timeout=timeout,
                    allow_redirects=False,
                )
            except requests.Timeout:  # before RequestException: ConnectTimeout is both
                raise SlackAPIError(method, "timeout", timed_out=True) from None
            except requests.RequestException as e:
                # str(e) can carry the URL or body: keep the type only.
                logger.warning("slack: %s network error (%s)", method, type(e).__name__)
                raise SlackAPIError(method, "network_error") from None
            logger.debug(
                "slack: %s -> HTTP %s in %.0fms",
                method,
                response.status_code,
                (time.monotonic() - started) * 1000,
            )
            payload, error, wait = _outcome(method, response, idempotent, attempt)
            if error is None:
                return payload
            remaining = self._remaining()
            if (
                wait is None
                or not retry
                or attempt >= self.max_retries
                or waited + wait > self.max_retry_wait
                # The sleep must leave room for the retried request itself.
                or (remaining is not None and wait + min(self.timeout, _REQUEST_FLOOR) > remaining)
            ):
                raise error
            attempt += 1
            logger.warning(
                "slack: %s failed (%s), retrying in %.0fs (retry %d/%d)",
                method,
                error.error,
                wait,
                attempt,
                self.max_retries,
            )
            self._sleep(wait)
            waited += wait


def _outcome(
    method: str, response: requests.Response, idempotent: bool, attempt: int
) -> tuple[dict[str, Any], SlackAPIError | None, float | None]:
    """``(payload, error, wait)`` for one response: the payload when ``ok``;
    otherwise the error and how long to wait before retrying it (None: do
    not retry). Never raises, whatever the body holds."""
    status = response.status_code
    body = parse_body(response)
    payload = body if isinstance(body, dict) else {}
    slack_error = payload.get("error")
    code = slack_error if isinstance(slack_error, str) and SAFE_CODE.fullmatch(slack_error) else None

    if code in RETRY_SOON_ERRORS:
        wait = retry_after_seconds(response) if response.headers.get("Retry-After") else RETRY_SOON_WAIT
        return payload, SlackAPIError(method, code, status=status, retry_after=wait), wait
    if status == 429 or (code in RATE_LIMIT_ERRORS):
        wait = retry_after_seconds(response)
        return payload, SlackAPIError(method, code or "ratelimited", status=status, retry_after=wait), wait
    server_error = status >= 500 or code in SERVER_ERRORS
    if not 200 <= status < 300 or not isinstance(body, dict) or payload.get("ok") is not True:
        if code is None:
            code = f"http_{status}" if not 200 <= status < 300 else (
                "invalid_response" if not isinstance(body, dict) else "unknown_error"
            )
        needed = payload.get("needed")
        error = SlackAPIError(
            method,
            code,
            status=status,
            needed=needed if isinstance(needed, str) and _SAFE_SCOPES.fullmatch(needed) else None,
        )
        retry_wait = _SERVER_ERROR_WAIT * (2**attempt) if server_error and idempotent else None
        return payload, error, retry_wait
    return payload, None, None


# --------------------------------------------------------------------------
# File bytes (plan §4.8): downloads from files.slack.com, v2 upload POSTs
# --------------------------------------------------------------------------

#: The only host a download may send the token to.
DOWNLOAD_HOST = "files.slack.com"
#: Redirects a download follows (by hand, so the token never leaves the host).
MAX_DOWNLOAD_REDIRECTS = 3
#: Read size while streaming a download.
DOWNLOAD_CHUNK = 64 * 1024


def _https_host(url: Any) -> str | None:
    """The host an ``https`` URL will really be sent to, lower-cased, or
    None when that isn't certain.

    The token's destination is decided the way requests decides it
    (``urllib3.util.parse_url``), and the URL is refused when the stdlib
    parser disagrees. ``https://evil.example\\@files.slack.com/x`` is
    ``files.slack.com`` to ``urlsplit`` but ``evil.example`` to urllib3. Also
    refused: any backslash, whitespace or control character, userinfo
    (``user@host``), and a port other than 443."""
    if not isinstance(url, str) or "\\" in url or any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        return None
    try:
        split = urllib.parse.urlsplit(url)
        parsed = urllib3.util.parse_url(url)
        split_port = split.port
    except (ValueError, urllib3.exceptions.LocationParseError):
        return None
    if split.scheme.lower() != "https" or (parsed.scheme or "").lower() != "https":
        return None
    if split.username is not None or split.password is not None or parsed.auth:
        return None
    if split_port not in (None, 443) or parsed.port not in (None, 443):
        return None
    host = (split.hostname or "").lower()
    if not host or host != (parsed.host or "").lower():
        return None
    return host


def is_slack_upload_url(url: Any) -> bool:
    """A pre-signed v2 upload URL: ``https`` on ``slack.com`` or a subdomain."""
    host = _https_host(url)
    return bool(host) and (host == "slack.com" or str(host).endswith(".slack.com"))


def _download(
    client: SlackWebClient, url: str, *, max_bytes: int
) -> tuple[bytes, str]:
    """GET a Slack-hosted file: ``(bytes, content type)``. The token goes only
    to ``https://files.slack.com``; redirects are followed by hand (at most
    :data:`MAX_DOWNLOAD_REDIRECTS`), each must be ``https``, and the token is
    dropped for any other host. The byte cap is enforced while reading and
    the time budget is checked between reads. Raises :class:`SlackAPIError`
    (method ``files.download``)."""
    method = "files.download"
    if _https_host(url) != DOWNLOAD_HOST:
        raise SlackAPIError(method, "invalid_download_url", status=200)
    for _ in range(MAX_DOWNLOAD_REDIRECTS + 1):
        host = _https_host(url)
        if host is None:
            raise SlackAPIError(method, "insecure_redirect", status=302)
        headers = {"User-Agent": USER_AGENT}
        if host == DOWNLOAD_HOST:
            headers["Authorization"] = f"Bearer {client._token}"
        response = client._send(method, "GET", url, headers=headers, stream=True)
        try:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not isinstance(location, str) or not location:
                    raise SlackAPIError(method, "bad_redirect", status=response.status_code)
                url = urllib.parse.urljoin(url, location)
                continue
            if response.status_code == 429:
                raise SlackAPIError(
                    method, "ratelimited", status=429, retry_after=retry_after_seconds(response)
                )
            if not 200 <= response.status_code < 300:
                raise SlackAPIError(method, f"http_{response.status_code}", status=response.status_code)
            content_type = str(response.headers.get("Content-Type") or "")
            return _read_capped(client, method, response, max_bytes), content_type
        finally:
            response.close()
    raise SlackAPIError(method, "too_many_redirects", status=302)


def _read_capped(client: SlackWebClient, method: str, response: requests.Response, max_bytes: int) -> bytes:
    """The streamed body: the byte cap enforced while reading, the budget
    checked between reads, and a transport failure mid-stream (a broken
    chunked encoding, a dropped connection, a read timeout — requests wraps
    that last one in ``ConnectionError``) reported as ``network_error`` /
    ``timeout``, never as a raw requests exception."""
    chunks: list[bytes] = []
    size = 0
    try:
        for chunk in response.iter_content(DOWNLOAD_CHUNK):
            size += len(chunk)
            if size > max_bytes:
                raise SlackAPIError(method, "file_too_large", status=response.status_code)
            chunks.append(chunk)
            remaining = client._remaining()
            if remaining is not None and remaining <= 0:
                raise SlackAPIError(method, "timeout", timed_out=True)
    except requests.RequestException as e:
        if isinstance(e, requests.Timeout) or any(
            isinstance(arg, urllib3.exceptions.ReadTimeoutError) for arg in e.args
        ):
            raise SlackAPIError(method, "timeout", timed_out=True) from None
        logger.warning("slack: %s failed mid-stream (%s)", method, type(e).__name__)
        raise SlackAPIError(method, "network_error", status=response.status_code) from None
    return b"".join(chunks)


def _upload(client: SlackWebClient, upload_url: str, data: bytes) -> None:
    """POST the bytes to a v2 ``upload_url``: pre-signed, so no token is
    sent; ``https`` on a ``slack.com`` host only, no redirects. Anything but
    HTTP 200 fails (method ``files.upload``)."""
    method = "files.upload"
    if not is_slack_upload_url(upload_url):
        raise SlackAPIError(method, "invalid_upload_url", status=200)
    response = client._send(
        method,
        "POST",
        upload_url,
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/octet-stream"},
        data=data,
    )
    try:
        if response.status_code == 429:
            raise SlackAPIError(method, "ratelimited", status=429, retry_after=retry_after_seconds(response))
        if response.status_code != 200:
            raise SlackAPIError(method, f"http_{response.status_code}", status=response.status_code)
    finally:
        response.close()


def parse_body(response: requests.Response) -> Any:
    """The JSON body, or None when there is none or it does not parse.
    Never raises (a pathological body can raise ``RecursionError``)."""
    try:
        return response.json()
    except Exception:  # noqa: BLE001 — not JSON, or not parseable at all
        return None


def retry_after_seconds(response: requests.Response) -> float:
    """Seconds from ``Retry-After``: finite, non-negative, capped at
    :data:`MAX_RETRY_AFTER`; :data:`DEFAULT_RETRY_AFTER` when absent or
    unusable. Never raises."""
    try:
        seconds = float(response.headers.get("Retry-After", ""))
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_RETRY_AFTER
    if not math.isfinite(seconds) or seconds < 0:
        return DEFAULT_RETRY_AFTER
    return min(seconds, MAX_RETRY_AFTER)


def form_value(value: Any) -> str:
    """One form field: booleans as ``true``/``false``; lists and objects as
    JSON (how Slack takes ``blocks`` or ``profile`` form-encoded)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value)
    return str(value)


def next_cursor(payload: Mapping[str, Any]) -> str | None:
    """``response_metadata.next_cursor``; None (the end) for an empty or
    missing one. ``has_more`` derives from this, never from the page size:
    Slack pages can come back short mid-collection."""
    meta = payload.get("response_metadata")
    cursor = meta.get("next_cursor") if isinstance(meta, Mapping) else None
    return cursor if isinstance(cursor, str) and cursor else None
