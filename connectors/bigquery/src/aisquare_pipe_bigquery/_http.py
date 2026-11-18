"""Transport helpers shared by the token exchange and the BigQuery client."""

from __future__ import annotations

import requests
import urllib3.exceptions


def timed_out(exc: BaseException) -> bool:
    """Whether a transport failure was a timeout. requests raises
    ``Timeout`` for a connect or read timeout before the headers, but a read
    timeout while it reads the BODY comes back as ``ConnectionError`` wrapping
    urllib3's ``ReadTimeoutError``; so look through the wrapped errors too."""
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(
            current, (requests.Timeout, urllib3.exceptions.TimeoutError, TimeoutError)
        ):
            return True
        stack.extend(a for a in current.args if isinstance(a, BaseException))
        for linked in (current.__cause__, current.__context__):
            if linked is not None:
                stack.append(linked)
    return False


def retry_after_seconds(value: object) -> float | None:
    """A ``Retry-After`` header in seconds (0-3600), else None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if 0 <= seconds <= 3600 else None


__all__ = ["retry_after_seconds", "timed_out"]
