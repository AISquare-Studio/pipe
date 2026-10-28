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
from collections.abc import Callable, Mapping
from typing import Any, Self

import requests

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

#: ``ok: false`` codes that are rate limits.
RATE_LIMIT_ERRORS = frozenset({"ratelimited", "rate_limited"})
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

    def call(
        self,
        method: str,
        args: Mapping[str, Any] | None = None,
        *,
        json_body: bool = False,
        idempotent: bool = False,
    ) -> dict[str, Any]:
        """Call one Web API method; return Slack's payload (``ok`` is true) or
        raise :class:`SlackAPIError`. ``None`` arguments are dropped.
        ``idempotent`` calls (reads) are also retried on server errors."""
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
                    "POST", url, data=body, headers=headers, timeout=timeout, allow_redirects=False
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
    code = slack_error if isinstance(slack_error, str) and slack_error else None

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
            needed=needed if isinstance(needed, str) and needed else None,
        )
        retry_wait = _SERVER_ERROR_WAIT * (2**attempt) if server_error and idempotent else None
        return payload, error, retry_wait
    return payload, None, None


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
