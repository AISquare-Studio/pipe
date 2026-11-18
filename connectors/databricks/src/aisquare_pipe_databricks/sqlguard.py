"""SELECT-only SQL: two independent checks, both before any HTTP call.

1. **The parser** (sqlglot, Databricks dialect): the text must parse into
   exactly ONE statement whose root is a query (``SELECT``, ``WITH ...
   SELECT``, a ``UNION``/``INTERSECT``/``EXCEPT`` of queries, or a
   parenthesised one), and no node anywhere in its tree may be a write, DDL,
   session or transaction statement (``INSERT``, ``UPDATE``, ``DELETE``,
   ``MERGE``, ``CREATE``, ``DROP``, ``ALTER``, ``SELECT ... INTO``, ``SET``,
   ``USE``, ``COPY``, ``CACHE``, ``REFRESH``, ``GRANT``, an unparsed
   ``Command``, ...). Text it can't parse is refused.
2. **The lexer** (no parser): with string literals, quoted identifiers and
   ``--`` / ``/* */`` comments removed, read with AND without backslash
   escapes, the text is one statement starting with ``SELECT``/``WITH``
   that names none of the write or session keywords.

Either one refusing refuses the statement (:class:`NotReadOnly`, message
naming why, never echoing the SQL). The parser understands the dialect; the
lexer catches anything the parser might read more generously than
Databricks does.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

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
            "Command",
            "Into",
            "Set",
            "Use",
            "Transaction",
            "Commit",
            "Rollback",
            "Copy",
            "LoadData",
            "Cache",
            "Uncache",
            "Refresh",
            "Grant",
            "Revoke",
            "Analyze",
            "Pragma",
            "Lock",
            "TruncateTable",
            "Describe",
        )
    )
    if isinstance(t, type)
)

_WRITE_KEYWORDS = frozenset(
    {
        "ALTER",
        "ANALYZE",
        "BEGIN",
        "CACHE",
        "CALL",
        "CLONE",
        "COMMIT",
        "COPY",
        "CREATE",
        "DECLARE",
        "DELETE",
        "DROP",
        "EXEC",
        "EXECUTE",
        "GRANT",
        "INSERT",
        "INTO",
        "LOAD",
        "MERGE",
        "MSCK",
        "OPTIMIZE",
        "PUT",
        "REFRESH",
        "REMOVE",
        "RENAME",
        "REPAIR",
        "RESET",
        "RESTORE",
        "REVOKE",
        "ROLLBACK",
        "SET",
        "TRUNCATE",
        "UNCACHE",
        "UNDROP",
        "UNSET",
        "UPDATE",
        "UPSERT",
        "USE",
        "VACUUM",
        "ZORDER",
    }
)
_FIRST_KEYWORDS = frozenset({"SELECT", "WITH"})
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_QUOTES = "'\"`"


class NotReadOnly(ValueError):
    """The SQL is not one read-only query. The message says why."""


def check_select_only(sql: str) -> str:
    """``sql`` stripped, if it is ONE read-only query (both checks pass);
    else :class:`NotReadOnly`."""
    if not isinstance(sql, str) or not sql.strip():
        raise NotReadOnly("the statement is empty")
    if len(sql) > MAX_SQL_CHARS:
        raise NotReadOnly(f"the statement is longer than {MAX_SQL_CHARS} characters")
    problem = _lexical(sql) or _parsed(sql)
    if problem is not None:
        raise NotReadOnly(f"only one SELECT (or WITH ... SELECT) may run: {problem}")
    return sql.strip()


# -- the parser ----------------------------------------------------------------------


def _parsed(sql: str) -> str | None:
    try:
        statements = [s for s in sqlglot.parse(sql, read="databricks") if s is not None]
    except SqlglotError:
        return "it does not parse as Databricks SQL"
    if len(statements) != 1:
        return "it is more than one statement" if statements else "it is empty"
    (root,) = statements
    if not isinstance(root, _QUERY_ROOTS):
        return f"it is a {root.key.upper()} statement, not a query"
    for node in root.walk():
        if isinstance(node, _WRITE_NODES):
            return f"it contains {node.key.upper()}"
    return None


# -- the lexer -----------------------------------------------------------------------


def _lexical(sql: str) -> str | None:
    for backslash in (True, False):
        stripped = _strip(sql, backslash_escapes=backslash)
        if stripped is None:
            return "it has an unterminated string or comment"
        statements = [part for part in stripped.split(";") if part.strip()]
        if len(statements) != 1:
            return "it is more than one statement" if statements else "it is empty"
        words = [w.upper() for w in _WORD.findall(statements[0])]
        if not words or words[0] not in _FIRST_KEYWORDS:
            return "it does not start with SELECT or WITH"
        written = sorted(set(words) & _WRITE_KEYWORDS)
        if written:
            return f"it names {written[0]}"
    return None


def _strip(sql: str, *, backslash_escapes: bool) -> str | None:
    """``sql`` with quoted text and ``--`` / ``/* */`` comments blanked; None
    when a quote or block comment never closes."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in _QUOTES:
            j = i + 1
            while True:
                if j >= n:
                    return None
                if backslash_escapes and sql[j] == "\\":
                    j += 2
                    continue
                if sql[j] == ch:
                    if j + 1 < n and sql[j + 1] == ch:
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(" ")
            i = j
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            out.append(" ")
            i = n if end < 0 else end + 1
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end < 0:
                return None
            out.append(" ")
            i = end + 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


__all__ = ["MAX_SQL_CHARS", "NotReadOnly", "check_select_only"]
