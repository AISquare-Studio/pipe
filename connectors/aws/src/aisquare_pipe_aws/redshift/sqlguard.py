"""SELECT-only SQL for Redshift: two independent checks, both before any AWS call.

1. **The lexer** (no parser). With string literals (``'...'``), quoted
   identifiers (``"..."``) and comments (``--``, ``/* */``) blanked, the
   text is ONE statement (a single trailing ``;`` is allowed and dropped)
   that starts with ``SELECT`` or ``WITH`` and names none of the write, DDL,
   session or side-effect words (``INSERT``, ``UPDATE``, ``DELETE``,
   ``MERGE``, ``CREATE``, ``DROP``, ``ALTER``, ``TRUNCATE``, ``COPY``,
   ``UNLOAD``, ``INTO``, ``GRANT``, ``CALL``, ``SET``, ``PG_TERMINATE_BACKEND``,
   ...), nor names a side-effect function even in double quotes
   (``"pg_terminate_backend"(1)``). Where two readers could disagree on where
   a token ends, it refuses rather than pick one:

   - a ``--`` comment ends at a CR or an LF (a bare CR too: Redshift's
     PostgreSQL lexer has ``newline [\\n\\r]``);
   - a ``/*`` inside a ``/* */`` comment is refused: PostgreSQL (and sqlglot)
     nest them, a non-nesting reader ends one at the first ``*/``;
   - a quoted string or identifier must end at the same place whether a
     backslash escapes the next character or not (``standard_conforming_strings``
     on or off, an ``E''`` string or not); if not, it is refused;
   - a ``$`` outside quotes is refused (``$$...$$`` and ``$tag$...$tag$``
     dollar quoting, ``$1`` parameters), and so are a backslash outside
     quotes and control characters other than tab, CR, LF and form feed;
   - a ``U&`` right before a quote is refused (``U&'...'``, ``U&"..."``):
     PostgreSQL 8.4+ decodes ``U&"pg_terminate_backen\\0064"`` to
     ``pg_terminate_backend``, a name the word check can't see;
   - a number is read as Redshift reads it, exponent included, before any
     word: ``1e5INTO`` is the number ``1e5``, then ``INTO`` (PostgreSQL
     before 15 splits it there), not the word ``e5INTO``.

2. **The parser** (sqlglot, Redshift dialect): the text parses into exactly
   ONE statement whose root is a query (``SELECT``, ``WITH ... SELECT``, a
   ``UNION``/``INTERSECT``/``EXCEPT`` of queries, or a parenthesised one),
   and no node anywhere in its tree is a write, DDL, session or transaction
   statement (an unparsed ``Command`` and ``SELECT ... INTO`` included), nor a
   call of a side-effect function. Text it can't parse is refused.

Either one refusing refuses the statement (:class:`NotReadOnly`, whose
message says why and never echoes the SQL). Neither logs any of the SQL.
sqlglot does log pieces of it while it parses: its "unsupported syntax"
warning, and "Invalid JSON path syntax. ... <the path>" at WARNING for a JSON
path it can't read (a key with a dash, a space or an open bracket, in
``JSON_EXTRACT``, ``JSON_EXTRACT_SCALAR`` or ``JSONB_EXISTS``), for
statements this guard admits. So this module parses through
:func:`aisquare.pipe.core.sql.parse_statements`, as every SQL guard does:
the warning is switched off, and a filter on the "sqlglot" logger drops its
records, in this context only, so they reach no handler, and not Python's
``lastResort`` either. Nothing is disabled globally, and sqlglot logs as
usual everywhere else. Any exception while it parses refuses, and a sqlglot
older than core's :data:`~aisquare.pipe.core.sql.SQLGLOT_FLOOR` (the
extras' floor; another package can bring one in) refuses every statement.

Behind these, Redshift itself: the database user the blob names should hold
only ``USAGE`` and ``SELECT`` grants (see the README), so a write that got
past both checks would still be refused by the database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from aisquare.pipe.core.sql import (
    SQLGLOT_FLOOR,
    SqlglotTooOld,
    Unparsable,
    parse_statements,
)
from sqlglot import exp
from sqlglot.dialects.redshift import Redshift

MAX_SQL_CHARS = 20_000

# Statement roots that read.
_QUERY_ROOTS: tuple[type[exp.Expression], ...] = tuple(
    t
    for t in (
        exp.Select,
        getattr(exp, "SetOperation", None),
        exp.Union,
        exp.Intersect,
        exp.Except,
        exp.Subquery,
    )
    if isinstance(t, type)
)
# Nodes no read-only statement holds, at any depth (looked up by name, so a
# sqlglot version without one simply has nothing to match).
_WRITE_NODES: tuple[type[exp.Expression], ...] = tuple(
    t
    for t in (
        getattr(exp, name, None)
        for name in (
            "Insert",
            "Update",
            "Delete",
            "Merge",
            "Create",
            "Drop",
            "Alter",
            "AlterTable",
            "AlterColumn",
            "RenameTable",
            "TruncateTable",
            "Command",
            "Into",
            "Set",
            "Declare",
            "DeclareItem",
            "Use",
            "Transaction",
            "Commit",
            "Rollback",
            "Copy",
            "LoadData",
            "Export",
            "Grant",
            "Revoke",
            "Analyze",
            "Describe",
            "Cache",
            "Uncache",
            "Refresh",
            "Pragma",
            "Lock",
        )
    )
    if isinstance(t, type)
)

#: Functions that change something outside the query: end or cancel a
#: session, change a setting. A read-only user's query has no use for them.
_SIDE_EFFECT_FUNCTIONS = frozenset(
    {"PG_TERMINATE_BACKEND", "PG_CANCEL_BACKEND", "SET_CONFIG"}
)

#: Words no read-only Redshift query has outside a string, identifier or
#: comment: statement words that write, change the session or move a
#: transaction, ``INTO`` (``SELECT ... INTO`` creates a table), ``UNLOAD``
#: (writes to S3) and the side-effect functions. Words that ARE legitimate
#: in a query are left out: END (CASE ... END), IF, REPLACE (a function),
#: and words that only ever begin a statement and are common column names
#: (COMMENT, START, CLOSE, RESET): the one-statement rule and the parser
#: refuse those statements.
_WRITE_KEYWORDS = frozenset(
    {
        "ABORT",
        "ALTER",
        "ANALYZE",
        "ATTACH",
        "BEGIN",
        "CALL",
        "CANCEL",
        "COMMIT",
        "COPY",
        "CREATE",
        "DEALLOCATE",
        "DECLARE",
        "DELETE",
        "DETACH",
        "DISCARD",
        "DROP",
        "EXECUTE",
        "FETCH",
        "GRANT",
        "INSERT",
        "INTO",
        "LOCK",
        "MERGE",
        "PREPARE",
        "REFRESH",
        "REVOKE",
        "ROLLBACK",
        "SAVEPOINT",
        "SET",
        "TRUNCATE",
        "UNLOAD",
        "UPDATE",
        "VACUUM",
        *_SIDE_EFFECT_FUNCTIONS,
    }
)
_FIRST_KEYWORDS = frozenset({"SELECT", "WITH"})
#: A number or a word, in one left-to-right scan, as Redshift's lexer
#: (PostgreSQL 8.0's) splits them: a number (``{integer}``, ``{decimal}`` or
#: ``{real}``: ``1``, ``1.5``, ``.5``, ``1.``, ``1e5``, ``1.5E-3``) is taken
#: whole before a word can start, so ``1e5INTO`` gives the number ``1e5``
#: and the word ``INTO`` (seq 13894 m1). A word holds digits after its first
#: letter, so ``col1e5into`` is one word, as it is one identifier there.
_NUMBER_OR_WORD = re.compile(
    r"(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|([A-Za-z_][A-Za-z0-9_]*)"
)
_QUOTES = "'\""
#: Control characters a statement may hold: tab, LF, CR and form feed.
_ALLOWED_CONTROLS = frozenset("\t\n\r\f")


class NotReadOnly(ValueError):
    """The SQL is not one read-only query. The message says why."""


_DIALECT = Redshift()


def check_select_only(sql: str) -> str:
    """The statement to send, if ``sql`` is ONE read-only query (both checks
    pass): stripped, and without a single trailing ``;``. Else
    :class:`NotReadOnly`."""
    if not isinstance(sql, str) or not sql.strip():
        raise NotReadOnly("the statement is empty")
    if len(sql) > MAX_SQL_CHARS:
        raise NotReadOnly(f"the statement is longer than {MAX_SQL_CHARS} characters")
    lexed = _lex(sql)
    problem = lexed.problem
    if problem is None:
        problem = _parsed_problem(lexed.statement)
    if problem is not None:
        raise NotReadOnly(f"only one SELECT (or WITH ... SELECT) may run: {problem}")
    return lexed.statement


def quote_identifier(name: str) -> str:
    """``name`` as a Redshift delimited identifier (``"..."``, inner quotes
    doubled), for SQL this package writes itself."""
    return '"' + name.replace('"', '""') + '"'


# -- the lexer -----------------------------------------------------------------------


@dataclass(frozen=True)
class _Lexed:
    statement: str
    problem: str | None


class _Unreadable(Exception):
    """Text the lexer won't vouch for: Redshift may read it some other way,
    or not at all. The message says why and never echoes the SQL."""


def _lex(sql: str) -> _Lexed:
    quoted_names: list[str] = []
    try:
        text, separators = _blank(sql, quoted_names=quoted_names)
    except _Unreadable as unreadable:
        return _Lexed(sql, str(unreadable))
    statement = sql
    if separators:
        last = separators[-1]
        if len(separators) > 1 or text[last + 1 :].strip():
            return _Lexed(sql, "it is more than one statement")
        # One trailing ';' (comments after it included): cut there.
        statement = sql[:last]
        text = text[:last]
    statement = statement.strip()
    words = [w.upper() for w in _NUMBER_OR_WORD.findall(text) if w]
    if not words:
        return _Lexed(statement, "it is empty")
    if words[0] not in _FIRST_KEYWORDS:
        return _Lexed(statement, "it does not start with SELECT or WITH")
    written = sorted(set(words) & _WRITE_KEYWORDS)
    if written:
        return _Lexed(
            statement,
            f"it names {written[0]} (quote a column or table with that name in double "
            "quotes)",
        )
    # A "..." name the word check can't see: "pg_terminate_backend"(1) and
    # pg_catalog."pg_terminate_backend"(1) end a session as surely.
    called = sorted({n.upper() for n in quoted_names} & _SIDE_EFFECT_FUNCTIONS)
    if called:
        return _Lexed(statement, f"it names {called[0]}")
    return _Lexed(statement, None)


#: Redshift's line ends, a bare CR included (PostgreSQL's lexer:
#: ``newline [\n\r]``, ``comment ("--"{non_newline}*)``).
_LINE_ENDS = "\r\n"
_LINE_END = re.compile(r"[\r\n]")


def _quoted_end(sql: str, start: int, *, backslash_escapes: bool) -> int:
    """Where the string or identifier opened at ``start`` ends (the index
    after its closing quote), reading a doubled quote as an escaped one and,
    when ``backslash_escapes``, a backslash as escaping the next character.
    :class:`_Unreadable` when it never closes."""
    quote = sql[start]
    j, n = start + 1, len(sql)
    while j < n:
        ch = sql[j]
        if backslash_escapes and ch == "\\":
            j += 2
            continue
        if ch == quote:
            if sql.startswith(quote * 2, j):
                j += 2
                continue
            return j + 1
        j += 1
    raise _Unreadable("it has an unterminated string or quoted identifier")


def _blank(sql: str, *, quoted_names: list[str] | None = None) -> tuple[str, list[int]]:
    """``sql`` with quoted text and comments replaced by spaces (same
    length, so positions line up), and the positions of the ``;`` outside
    them. Each ``"..."`` identifier's name (``""`` read as ``"``) is appended
    to ``quoted_names``. :class:`_Unreadable` when a quote or block comment never closes,
    a quoted token's end depends on how backslashes are read, a block
    comment holds a ``/*``, a ``U&`` opens a quote, or a ``$``, a backslash
    or a control character is outside quotes."""
    for ch in sql:
        if ch < " " and ch not in _ALLOWED_CONTROLS:
            raise _Unreadable("it has a control character")
    out = list(sql)
    separators: list[int] = []
    i, n = 0, len(sql)

    def blank(start: int, end: int) -> None:
        for k in range(start, end):
            if out[k] not in _LINE_ENDS:
                out[k] = " "

    while i < n:
        ch = sql[i]
        if ch in _QUOTES:
            if i >= 2 and sql[i - 1] == "&" and sql[i - 2] in "uU":
                # U&'...' / U&"...": \XXXX escapes the word check can't see
                # through (U&"pg_terminate_backen\0064", seq 13894 n1).
                raise _Unreadable(
                    "it has a U& Unicode escape (U&'...' or U&\"...\" is refused)"
                )
            plain = _quoted_end(sql, i, backslash_escapes=False)
            escaped = _quoted_end(sql, i, backslash_escapes=True)
            if plain != escaped:
                raise _Unreadable(
                    "a backslash in a string or quoted identifier makes where it ends "
                    "depend on the reader (double a quote to escape it)"
                )
            if ch == '"' and quoted_names is not None:
                quoted_names.append(sql[i + 1 : plain - 1].replace('""', '"'))
            blank(i, plain)
            i = plain
        elif sql.startswith("--", i):
            # To the line's end: the first CR or LF.
            line_end = _LINE_END.search(sql, i)
            end = n if line_end is None else line_end.start()
            blank(i, end)
            i = end
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end < 0:
                raise _Unreadable("it has an unterminated /* */ comment")
            # Up to the closer's '*' included: in '/* a /*/', a nesting reader
            # takes '/*' first and the comment runs on.
            if "/*" in sql[i + 2 : end + 1]:
                raise _Unreadable(
                    "it has a /* inside a /* */ comment (readers disagree on nesting)"
                )
            blank(i, end + 2)
            i = end + 2
        elif ch == "$":
            raise _Unreadable(
                "it has a $ outside quotes (dollar quoting and $n parameters are refused; "
                "quote a name holding $ in double quotes)"
            )
        elif ch == "\\":
            raise _Unreadable("it has a backslash outside quotes")
        else:
            if ch == ";":
                separators.append(i)
            i += 1
    return "".join(out), separators


# -- the parser ----------------------------------------------------------------------


def _parse(sql: str) -> list[exp.Expression]:
    """Every statement in ``sql`` (sqlglot, Redshift dialect, through
    :func:`~aisquare.pipe.core.sql.parse_statements`: nothing sqlglot logs
    while it parses gets out)."""
    return parse_statements(sql, _DIALECT).statements


def _function_name(node: exp.Expression) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name).upper()
    if isinstance(node, exp.Func):
        return node.sql_name().upper()
    return ""


def _parsed_problem(sql: str) -> str | None:
    try:
        statements = _parse(sql)
    except SqlglotTooOld as old:
        raise NotReadOnly(
            f"{old}; the host must install aisquare-pipe[redshift] as declared"
        ) from None
    except Unparsable:
        return "it does not parse as Redshift SQL"
    if len(statements) != 1:
        return "it is more than one statement" if statements else "it is empty"
    (root,) = statements
    if not isinstance(root, _QUERY_ROOTS):
        return f"it is a {root.key.upper()} statement, not a query"
    for node in root.walk():
        if isinstance(node, _WRITE_NODES):
            return f"it contains {node.key.upper()}"
        if _function_name(node) in _SIDE_EFFECT_FUNCTIONS:
            return f"it calls {_function_name(node)}"
    return None


__all__ = [
    "MAX_SQL_CHARS",
    "SQLGLOT_FLOOR",
    "NotReadOnly",
    "check_select_only",
    "quote_identifier",
]
