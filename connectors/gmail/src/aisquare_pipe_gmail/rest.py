"""Gmail v1 REST client for the Gmail toolkit.

This module is the package's only HTTP boundary (tests mock the
``requests.Session`` handed to it). One :class:`GmailRestClient` serves one
tool call with one access token. The token rides the ``Authorization``
header only: it never appears in a URL, a log line, an exception message or
a repr.

Non-2xx answers raise :class:`GmailAPIError` carrying the status, Google's own
message, its first error ``reason`` and whether the 403 was a missing OAuth
scope; the toolkit turns that into a failed ``ToolResult``.

Rate limits: a 429, or a 403 whose reason is ``rateLimitExceeded`` /
``userRateLimitExceeded``, is retried up to ``max_retries`` times with
exponential backoff (1s, 2s, 4s, ... — or ``Retry-After`` when Google sends
one). A 5xx on a GET is retried the same way; a 5xx on a POST is not (a send
that failed half-way must not go out twice). The total sleep per call is
capped at ``max_retry_wait`` seconds; a longer wait fails at once (with
``GmailAPIError.retry_after`` set).

Time budget: with ``budget`` (seconds; the host's ``config["timeout_seconds"]``)
the whole call — every attempt and every retry sleep — fits inside it. Each
request's timeout shrinks to what is left, a retry that would sleep past it
fails at once, and running out raises a ``timed_out`` error.

Messages: :meth:`GmailRestClient.post_message` sends a raw RFC 2822 message
as ``{"raw": base64url}`` JSON up to :data:`JSON_RAW_MAX` bytes, and larger
ones (attachments) as one ``multipart/related`` upload, which Gmail accepts
up to :data:`MESSAGE_MAX` bytes.
"""

from __future__ import annotations

import base64
import json as jsonlib
import logging
import math
import re
import secrets
import time
import urllib.parse
from collections.abc import Callable, Mapping
from typing import Any, Self

import requests

logger = logging.getLogger("aisquare.pipe.gmail")

API_BASE = "https://gmail.googleapis.com/gmail/v1/users"
UPLOAD_BASE = "https://gmail.googleapis.com/upload/gmail/v1/users"
USER_AGENT = "aisquare-pipe-gmail"

DEFAULT_TIMEOUT = 30.0
#: Per-request timeout for message bodies (attachments, large sends).
MEDIA_TIMEOUT = 120.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_MAX_RETRY_WAIT = 60.0

#: Largest raw message sent inline as JSON; bigger ones go as an upload.
JSON_RAW_MAX = 4 * 1024 * 1024
#: Gmail's limit for a message sent through the upload endpoint.
MESSAGE_MAX = 35 * 1024 * 1024

#: 403/429 reasons that mean "slow down" (retried); every other 403 is final.
RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})
#: Reasons Google gives for a token that lacks the scope a method needs.
SCOPE_REASONS = frozenset({"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"})
_RETRYABLE_SERVER_ERRORS = frozenset({500, 502, 503, 504})
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]*={0,2}$")
_BACKOFF_BASE = 1.0
_MAX_MESSAGE = 500
#: Time a retry keeps for its request after a backoff sleep (budgeted calls).
_REQUEST_ROOM = 2.0


class GmailAPIError(Exception):
    """A non-2xx Google answer (``status``) or a transport failure (``status=0``).

    ``message`` is Google's own error text; ``reason`` its first
    ``errors[].reason`` (``notFound``, ``failedPrecondition``,
    ``userRateLimitExceeded``, ...); ``retry_after`` is set when the failure is
    a rate limit, in seconds; ``timed_out`` when Google did not answer in time
    (or the budget ran out); ``insufficient_scope`` when a 403 says the token
    was not granted the scope the method needs.
    """

    def __init__(
        self,
        status: int,
        message: str,
        *,
        reason: str | None = None,
        retry_after: float | None = None,
        timed_out: bool = False,
        insufficient_scope: bool = False,
    ) -> None:
        super().__init__(f"{status}: {message}" if status else message)
        self.status = status
        self.message = message
        self.reason = reason
        self.retry_after = retry_after
        self.timed_out = timed_out
        self.insufficient_scope = insufficient_scope


class GmailRestClient:
    """Bearer-token client for the Gmail v1 API (one per call)."""

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
        timeout: float | None = None,
    ) -> requests.Response:
        """Send one request (``path`` is relative to the API base, or to the
        upload base when ``upload``); return the 2xx response or raise
        :class:`GmailAPIError`."""
        base = self.upload_url if upload else self.base_url
        all_headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": USER_AGENT,
            **(headers or {}),
        }
        per_request = self.timeout if timeout is None else timeout
        waited = 0.0
        attempt = 0
        while True:
            remaining = self._remaining()
            if remaining is not None and remaining <= 0:
                raise GmailAPIError(
                    0, f"Gmail call ran out of its {self.budget:g}s time budget", timed_out=True
                )
            request_timeout = per_request if remaining is None else min(per_request, remaining)
            started = time.monotonic()
            try:
                response = self._session.request(
                    method,
                    base + path,
                    params=dict(params) if params else None,
                    json=json,
                    data=data,
                    headers=dict(all_headers),
                    timeout=request_timeout,
                )
            except requests.Timeout as e:  # before RequestException: ConnectTimeout is both
                raise GmailAPIError(
                    0,
                    f"Gmail did not answer within {round(request_timeout, 1):g}s ({type(e).__name__})",
                    timed_out=True,
                ) from None
            except requests.RequestException as e:
                # str(e) can carry the URL (and so argument values): name the type only.
                raise GmailAPIError(0, f"network error talking to Gmail ({type(e).__name__})") from None
            logger.debug(
                "gmail: %s -> %s in %.0fms",
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
                raise api_error(response, retry_after=wait if limited else None)
            attempt += 1
            logger.warning(
                "gmail: HTTP %s, retrying in %.0fs (retry %d/%d)",
                response.status_code,
                wait,
                attempt,
                self.max_retries,
            )
            self._sleep(wait)
            waited += wait

    def get(self, path: str, params: Mapping[str, Any] | None = None, **kw: Any) -> requests.Response:
        return self.request("GET", path, params=params, **kw)

    def post(self, path: str, json: Any = None, **kw: Any) -> requests.Response:
        return self.request("POST", path, json=json, **kw)

    # -------------------------------------------------------------- messages

    def post_message(
        self, path: str, raw: bytes, *, thread_id: str | None = None, draft: bool = False
    ) -> dict[str, Any]:
        """POST a raw RFC 2822 message to ``path`` (``/<user>/messages/send``,
        or ``/<user>/drafts`` with ``draft``), in ``thread_id`` when given: as
        JSON ``raw`` when small, else as a multipart upload."""
        if len(raw) > MESSAGE_MAX:
            raise ValueError(
                f"the message is {len(raw):,} bytes, over Gmail's {MESSAGE_MAX:,}-byte limit"
            )
        message: dict[str, Any] = {"threadId": thread_id} if thread_id else {}
        if len(raw) <= JSON_RAW_MAX:
            message["raw"] = base64url(raw)
            body = {"message": message} if draft else message
            return json_object(self.post(path, json=body, timeout=MEDIA_TIMEOUT))
        metadata = {"message": message} if draft else message
        boundary = f"aisquare-pipe-{secrets.token_hex(12)}"
        body_bytes, header = multipart_body(metadata, raw, boundary)
        response = self.request(
            "POST",
            path,
            params={"uploadType": "multipart"},
            data=body_bytes,
            headers={"Content-Type": header},
            upload=True,
            timeout=MEDIA_TIMEOUT,
        )
        return json_object(response)


# --------------------------------------------------------------------------
# Request / response helpers
# --------------------------------------------------------------------------


def quote_segment(value: str, what: str = "Gmail id") -> str:
    """A Gmail id (message, thread, label, draft, attachment) or user id as one
    URL path segment. Anything outside ``[A-Za-z0-9_-]`` is encoded, and a
    ``.``/``..`` id — which the HTTP stack would resolve as a dot segment and
    so reroute the call — is refused."""
    text = str(value).strip()
    if text in ("", ".", ".."):
        raise ValueError(f"a {what} must not be empty, '.' or '..'")
    return urllib.parse.quote(text, safe="")


def base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def decode_base64url(text: str) -> bytes:
    """Gmail's base64url (padding optional); ValueError when it is not."""
    cleaned = "".join(str(text).split())
    if not _BASE64URL.match(cleaned):
        raise ValueError("not base64url")
    try:
        return base64.urlsafe_b64decode(cleaned.rstrip("=") + "=" * (-len(cleaned.rstrip("=")) % 4))
    except (ValueError, TypeError) as e:
        raise ValueError("not base64url") from e


def multipart_body(metadata: Mapping[str, Any], message: bytes, boundary: str) -> tuple[bytes, str]:
    """A ``multipart/related`` upload body (JSON metadata part + the
    ``message/rfc822`` part) and its ``Content-Type`` header."""
    head = (
        f"--{boundary}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{jsonlib.dumps(dict(metadata))}\r\n"
        f"--{boundary}\r\n"
        "Content-Type: message/rfc822\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    return head + message + tail, f"multipart/related; boundary={boundary}"


def json_body(response: requests.Response) -> Any:
    """Parsed JSON body; ``None`` for an empty body (e.g. 204)."""
    if response.status_code == 204 or not response.content:
        return None
    try:
        return response.json()
    except Exception:  # noqa: BLE001 — ValueError, or RecursionError on absurd nesting
        raise GmailAPIError(response.status_code, "Gmail returned a response that is not JSON") from None


def json_object(response: requests.Response) -> dict[str, Any]:
    body = json_body(response)
    return body if isinstance(body, dict) else {}


def _error_payload(response: requests.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 — any unparseable body (RecursionError included) has no details
        return {}
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, dict) else {}


def error_details(response: requests.Response) -> tuple[str, str | None, bool]:
    """Google's error ``message``, first ``errors[].reason``, and whether the
    error says the token lacks a scope (an ``insufficientPermissions`` reason,
    an ``ACCESS_TOKEN_SCOPE_INSUFFICIENT`` ErrorInfo, or a
    ``WWW-Authenticate: ... error="insufficient_scope"`` header)."""
    error = _error_payload(response)
    message = str(error.get("message") or "")
    reason: str | None = None
    reasons: set[str] = set()
    errors = error.get("errors")
    if isinstance(errors, list):
        for item in errors:
            if isinstance(item, dict) and item.get("reason"):
                reasons.add(str(item["reason"]))
                if reason is None:
                    reason = str(item["reason"])
                    message = message or str(item.get("message") or "")
    details = error.get("details")
    if isinstance(details, list):
        reasons.update(str(d.get("reason")) for d in details if isinstance(d, dict) and d.get("reason"))
    challenge = str(response.headers.get("WWW-Authenticate") or "")
    scope = response.status_code == 403 and (
        bool(reasons & SCOPE_REASONS)
        or "insufficient_scope" in challenge
        or "insufficient authentication scopes" in message.lower()
    )
    message = message or response.reason or "HTTP error"
    return message[:_MAX_MESSAGE], reason, scope


def api_error(response: requests.Response, *, retry_after: float | None = None) -> GmailAPIError:
    message, reason, scope = error_details(response)
    return GmailAPIError(
        response.status_code,
        message,
        reason=reason,
        retry_after=retry_after,
        insufficient_scope=scope,
    )


def is_rate_limited(response: requests.Response) -> bool:
    """A 429, or a 403 whose reason is a rate limit (not a daily quota)."""
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    _, reason, _ = error_details(response)
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
