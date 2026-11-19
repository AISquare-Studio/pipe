"""Helpers every connector's SQL guard shares.

A guard parses only through :func:`parse_statements` (tests/test_sql_guards.py
checks every connector module that imports sqlglot): the floor check, a
quiet tokenize and parse, and any exception a refusal, in one place, so a
new guard can't forget one.

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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

#: The logger every sqlglot module logs through (at 26.25 and 27.x).
SQLGLOT_LOGGER = "sqlglot"
#: The oldest sqlglot a SQL guard runs on: the floor the root pyproject's
#: extras give sqlglot (every extra that names it gives the same specifier:
#: scripts/dep_floor.py). tests/test_sql_guards.py fails when the two
#: differ, so a floor raised in the pyproject is raised here in the same
#: change, and no guard keeps a copy. It is not read from the installed
#: metadata at run time: which metadata Python finds depends on sys.path (a
#: stale build's ``*.egg-info`` in the working directory wins over the
#: installed ``*.dist-info``).
SQLGLOT_FLOOR: tuple[int, ...] = (26, 25)

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


class SqlglotTooOld(ValueError):
    """The installed sqlglot is older than the floor: no statement may run
    (a host can get an older one from another package; the registry checks
    names, not versions). A guard adds which extra to install."""

    def __init__(self, installed: str, floor: tuple[int, ...]) -> None:
        self.installed = installed
        self.floor = floor
        super().__init__(
            "the SQL guard needs sqlglot "
            + ".".join(map(str, floor))
            + " or later and this host has an older one, so no statement may run"
        )


class Unparsable(ValueError):
    """sqlglot could not read the text: it raised, a parse error or a bug in
    sqlglot alike (either refuses, never admits). Carries no SQL."""


@dataclass(frozen=True)
class ParsedSQL:
    """sqlglot's reading of a text: its ``tokens`` and every statement in it
    (``statements``, empty ones dropped)."""

    tokens: list[Any]
    statements: list[Any]


_QUIET_PARSERS: dict[type, type] = {}


def quiet_parser_class(dialect: Any) -> type:
    """``dialect``'s parser class without its "contains unsupported syntax"
    warning, which logs the SQL text (:func:`quiet_sqlglot` drops it too; a
    statement sqlglot doesn't support parses as a ``Command``, which a guard
    refuses anyway). One class per dialect, made once."""
    base: type = type(dialect).Parser
    quiet = _QUIET_PARSERS.get(base)
    if quiet is None:
        with _ATTACH_LOCK:
            quiet = _QUIET_PARSERS.get(base)
            if quiet is None:
                quiet = type(
                    f"Quiet{base.__qualname__.replace('.', '')}",
                    (base,),
                    {"_warn_unsupported": lambda self: None, "__module__": __name__},
                )
                _QUIET_PARSERS[base] = quiet
    return quiet


def parse_statements(
    sql: str,
    dialect: Any,
    *,
    floor: tuple[int, ...] | None = None,
    check_tokens: Callable[[list[Any]], None] | None = None,
) -> ParsedSQL:
    """``sql`` read by sqlglot in ``dialect`` (a sqlglot ``Dialect``), the
    one way a guard parses:

    1. the installed sqlglot must be at least ``floor`` (default
       :data:`SQLGLOT_FLOOR`), else :class:`SqlglotTooOld`;
    2. tokenize, then parse, inside :func:`quiet_sqlglot`, with
       :func:`quiet_parser_class`'s parser;
    3. any exception from either is :class:`Unparsable` (raised from None:
       sqlglot's messages quote the SQL).

    ``check_tokens``, when given, is called with the tokens after step 2
    tokenizes and before it parses, for a guard whose first check reads
    sqlglot's tokens (Databricks: where they and Spark's lexer read the text
    apart), so a text refused there is refused for that reason even when it
    would not parse either. It refuses by raising; what it raises propagates
    as it is (never as :class:`Unparsable`), and nothing is parsed then. It is
    not called when tokenizing fails. It gets a copy of the list of tokens
    (clearing or reordering it changes nothing parsed), but the ``Token``
    objects in it are the ones parsed: it must not change them (their text,
    type or comments), or what is parsed is not what sqlglot read.

    sqlglot is imported here, not at the top: core does not depend on it."""
    import sqlglot

    wanted = SQLGLOT_FLOOR if floor is None else floor
    installed = str(getattr(sqlglot, "__version__", ""))
    if not sqlglot_at_least(installed, wanted):
        raise SqlglotTooOld(installed, wanted)
    try:
        with quiet_sqlglot():
            tokens = list(dialect.tokenize(sql))
    except Exception:  # noqa: BLE001 — a tokenizer bug refuses, never admits
        raise Unparsable("sqlglot could not parse it") from None
    if check_tokens is not None:
        # A copy of the list only: the Token objects are shared, so
        # check_tokens must not change them (#50 review seq 14161 n3).
        check_tokens(list(tokens))
    try:
        with quiet_sqlglot():
            parser = quiet_parser_class(dialect)(dialect=dialect)
            statements = [s for s in parser.parse(tokens, sql) if s is not None]
    except Exception:  # noqa: BLE001 — a parser bug refuses, never admits
        raise Unparsable("sqlglot could not parse it") from None
    return ParsedSQL(tokens=list(tokens), statements=statements)


__all__ = [
    "SQLGLOT_FLOOR",
    "SQLGLOT_LOGGER",
    "ParsedSQL",
    "SqlglotTooOld",
    "Unparsable",
    "parse_statements",
    "quiet_parser_class",
    "quiet_sqlglot",
    "sqlglot_at_least",
    "sqlglot_version",
]
