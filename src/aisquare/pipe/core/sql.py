"""Helpers every connector's SQL guard shares.

sqlglot logs pieces of the SQL it parses: "Invalid JSON path syntax. ...
<the path>" at WARNING for any JSON key with a dash (``GET_PATH(v, 'a-b')``,
``JSON_EXTRACT(p, 'member.ssn-123')``), for statements a guard admits, and
its parser's "unsupported syntax" and error-level messages quote the SQL
too. With no logging configured at all, Python's ``logging.lastResort``
prints WARNING records to stderr. Caller SQL is customer data, so a guard
parses inside :func:`quiet_sqlglot`.

:func:`sqlglot_at_least` lets a guard fail closed on a sqlglot older than
the floor its extra declares (a host can get an older one from another
package; the registry checks names, not versions).

This module never imports sqlglot: core does not depend on it.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

#: The logger every sqlglot module logs through (at 26.25 and 27.x).
SQLGLOT_LOGGER = "sqlglot"

_QUIET: ContextVar[int] = ContextVar("aisquare_pipe_quiet_sqlglot", default=0)
_ATTACH_LOCK = threading.Lock()


class _DropWhileQuiet(logging.Filter):
    """Drops a record while :func:`quiet_sqlglot` runs in the context that
    logs it; passes every other record."""

    def filter(self, record: logging.LogRecord) -> bool:
        return _QUIET.get() == 0


_FILTER = _DropWhileQuiet()


def _sqlglot_loggers() -> list[logging.Logger]:
    """The "sqlglot" logger and any "sqlglot.*" one that exists. A logger's
    filters see only what is logged through it, not what its children
    propagate, so each gets the filter."""
    names = [SQLGLOT_LOGGER]
    names.extend(
        name
        for name in list(logging.Logger.manager.loggerDict)
        if name.startswith(SQLGLOT_LOGGER + ".")
    )
    return [logging.getLogger(name) for name in names]


def _attach() -> None:
    # Checked on every use: a host that resets a logger's filters (dictConfig,
    # filters.clear()) does not bring the leak back.
    for logger in _sqlglot_loggers():
        if _FILTER not in logger.filters:
            with _ATTACH_LOCK:
                if _FILTER not in logger.filters:
                    logger.addFilter(_FILTER)


@contextmanager
def quiet_sqlglot() -> Iterator[None]:
    """Nothing sqlglot logs while the block runs gets out, at any level.

    A filter on the "sqlglot" loggers (each that exists when the block
    starts; sqlglot makes them when its modules are imported) drops their
    records in THIS context only (a ContextVar: another thread or asyncio task logging through
    sqlglot meanwhile is untouched, and blocks nest). A logger's filters run
    before any handler, so ``logging.lastResort`` never sees the records
    either. Nothing is disabled globally, and sqlglot logs as usual outside
    the block."""
    _attach()
    token = _QUIET.set(_QUIET.get() + 1)
    try:
        yield
    finally:
        _QUIET.reset(token)


_VERSION_PART = re.compile(r"\d+")


def sqlglot_version(version: str) -> tuple[int, ...]:
    """``"26.25.0"`` (or ``"27.0.1.dev3"``) as ``(26, 25, 0)``: the leading
    dot-separated numbers."""
    parts: list[int] = []
    for piece in version.split("."):
        match = _VERSION_PART.match(piece)
        if match is None:
            break
        parts.append(int(match.group()))
        if match.end() != len(piece):
            break
    return tuple(parts)


def sqlglot_at_least(installed: str, floor: tuple[int, ...]) -> bool:
    """True when the installed sqlglot version is at or above ``floor``. An
    unreadable version is not."""
    parsed = sqlglot_version(installed)
    return bool(parsed) and parsed >= floor


__all__ = [
    "SQLGLOT_LOGGER",
    "quiet_sqlglot",
    "sqlglot_at_least",
    "sqlglot_version",
]
