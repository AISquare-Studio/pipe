"""SELECT-only SQL: two independent checks, both before any API call.

1. **The lexer** (no parser), read as BigQuery reads GoogleSQL (ZetaSQL's
   tokenizer): with string literals (``'...'``, ``"..."``, ``'''...'''``,
   ``\"\"\"...\"\"\"``, backslash escapes always on), quoted identifiers
   (`````...`````) and comments (``--``, ``#``, ``/* */``) blanked, the text
   is ONE statement (a single trailing ``;`` is allowed and dropped) that
   starts with ``SELECT`` or ``WITH`` and names none of the write, DDL or
   scripting keywords (``INSERT``, ``UPDATE``, ``DELETE``, ``MERGE``,
   ``CREATE``, ``DROP``, ``ALTER``, ``TRUNCATE``, ``DECLARE``, ``SET``,
   ``BEGIN``, ``CALL``, ``EXECUTE``, ``EXPORT``, ``LOAD``, ``ASSERT``,
   ``GRANT``, ...). As in ZetaSQL, a line ends at a CR or an LF (a bare CR
   too): a ``--`` or ``#`` comment ends at either, and a one-line string or
   a backtick identifier holding either is refused (BigQuery reads no token
   there). GoogleSQL ``/* */`` comments don't nest, so the first ``*/`` ends
   one; a ``/*`` inside one is refused, because a reader that nests them
   (sqlglot 25.3 to 26.24 did) hides text BigQuery runs.
2. **The parser** (sqlglot, BigQuery dialect): the text parses into exactly
   ONE statement whose root is a query (``SELECT``, ``WITH ... SELECT``, a
   ``UNION``/``INTERSECT``/``EXCEPT`` of queries, or a parenthesised one),
   and no node anywhere in its tree is a write, DDL, scripting, session or
   transaction statement (an unparsed ``Command`` included). Text it can't
   parse is refused.

Either one refusing refuses the statement (:class:`NotReadOnly`, whose
message says why and never echoes the SQL). Neither ever logs: sqlglot's
"unsupported syntax" warning, which quotes the SQL, is switched off for
this parser.

Google adds its own layers behind these (see the README): the token's scope
is ``bigquery.readonly``, the service account's roles grant no write, and the
dry run's ``statementType`` must be ``SELECT``.

:func:`table_references` reads, from a checked statement, the tables it
names (CTE names left out), for the source's ``lastModifiedTime`` marker.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlglot import exp
from sqlglot.dialects.bigquery import BigQuery
from sqlglot.errors import SqlglotError

from aisquare_pipe_bigquery.credentials import PROJECT_ID
from aisquare_pipe_bigquery.refs import DATASET_ID, TABLE_ID, TableRef

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

#: Words no read-only GoogleSQL query has outside a string, identifier or
#: comment. Words that ARE legitimate in a query are left out: END (CASE
#: ... END), FOR (FOR SYSTEM_TIME AS OF), IF, REPEAT and REPLACE (functions,
#: and SELECT * REPLACE), EXCEPT (SELECT * EXCEPT).
_WRITE_KEYWORDS = frozenset(
    {
        "ALTER",
        "ASSERT",
        "BEGIN",
        "BREAK",
        "CALL",
        "COMMIT",
        "CONTINUE",
        "CREATE",
        "DECLARE",
        "DELETE",
        "DROP",
        "EXECUTE",
        "EXPORT",
        "GRANT",
        "IMMEDIATE",
        "INSERT",
        "INTO",
        "ITERATE",
        "LEAVE",
        "LOAD",
        "LOOP",
        "MERGE",
        "RAISE",
        "RETURN",
        "REVOKE",
        "ROLLBACK",
        "SET",
        "TRANSACTION",
        "TRUNCATE",
        "UNDROP",
        "UPDATE",
        "WHILE",
    }
)
_FIRST_KEYWORDS = frozenset({"SELECT", "WITH"})
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_QUOTES = "'\"`"


class NotReadOnly(ValueError):
    """The SQL is not one read-only query. The message says why."""


class _QuietParser(BigQuery.Parser):
    """sqlglot's BigQuery parser without its "contains unsupported syntax"
    warning, which logs the SQL text. The statement is refused anyway: an
    unsupported statement parses as a ``Command``."""

    def _warn_unsupported(self) -> None:
        return None


_DIALECT = BigQuery()


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


# -- the lexer -----------------------------------------------------------------------


@dataclass(frozen=True)
class _Lexed:
    statement: str
    problem: str | None


class _Unreadable(Exception):
    """Text the lexer won't vouch for: BigQuery reads it some other way, or
    not at all. The message says why and never echoes the SQL."""


def _lex(sql: str) -> _Lexed:
    try:
        text, separators = _blank(sql)
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
    words = [w.upper() for w in _WORD.findall(text)]
    if not words:
        return _Lexed(statement, "it is empty")
    if words[0] not in _FIRST_KEYWORDS:
        return _Lexed(statement, "it does not start with SELECT or WITH")
    written = sorted(set(words) & _WRITE_KEYWORDS)
    if written:
        return _Lexed(
            statement,
            f"it names {written[0]} (quote a column or table with that name in "
            "backticks)",
        )
    return _Lexed(statement, None)


#: GoogleSQL's line ends, a bare CR included. ZetaSQL's tokenizer has
#: ``dash_comment = --[^\r\n]*(\r|\n|\r\n)?`` (``#`` comments the same), and
#: its one-line strings and backtick identifiers exclude both (``[^'\\\n\r]``).
_LINE_ENDS = "\r\n"
_LINE_END = re.compile(r"[\r\n]")


def _blank(sql: str) -> tuple[str, list[int]]:
    """``sql`` with quoted text and comments replaced by spaces (same
    length, so positions line up), and the positions of the ``;`` outside
    them. :class:`_Unreadable` when a quote or block comment never closes,
    a one-line string or backtick identifier holds a CR or LF, or a block
    comment holds a ``/*``."""
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
            triple = ch != "`" and sql.startswith(ch * 3, i)
            closer = ch * 3 if triple else ch
            j = i + len(closer)
            while True:
                if j >= n:
                    raise _Unreadable(
                        "it has an unterminated string or quoted identifier"
                    )
                if sql[j] == "\\":
                    # GoogleSQL escapes are on in every quoted form; one takes
                    # the next character, or a CR LF pair.
                    j += 3 if sql.startswith("\r\n", j + 1) else 2
                    continue
                if sql.startswith(closer, j):
                    j += len(closer)
                    break
                if not triple and sql[j] in _LINE_ENDS:
                    raise _Unreadable(
                        "it has a string or quoted identifier that runs past a line end"
                    )
                j += 1
            blank(i, j)
            i = j
        elif sql.startswith("--", i) or ch == "#":
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
                    "it has a /* inside a /* */ comment (GoogleSQL comments don't nest)"
                )
            blank(i, end + 2)
            i = end + 2
        else:
            if ch == ";":
                separators.append(i)
            i += 1
    return "".join(out), separators


# -- the parser ----------------------------------------------------------------------


def _parse(sql: str) -> list[exp.Expression]:
    """Every statement in ``sql`` (sqlglot, BigQuery dialect, silently)."""
    tokens = _DIALECT.tokenize(sql)
    parser = _QuietParser(dialect=_DIALECT)
    return [s for s in parser.parse(tokens, sql) if s is not None]


def _parsed_problem(sql: str) -> str | None:
    try:
        statements = _parse(sql)
    except SqlglotError:
        return "it does not parse as GoogleSQL"
    except Exception:  # noqa: BLE001 — a parser bug refuses, never admits
        return "it does not parse as GoogleSQL"
    if len(statements) != 1:
        return "it is more than one statement" if statements else "it is empty"
    (root,) = statements
    if not isinstance(root, _QUERY_ROOTS):
        return f"it is a {root.key.upper()} statement, not a query"
    for node in root.walk():
        if isinstance(node, _WRITE_NODES):
            return f"it contains {node.key.upper()}"
    return None


# -- the tables a query reads --------------------------------------------------------


def table_references(
    sql: str, *, default_dataset: tuple[str | None, str] | None = None
) -> tuple[list[TableRef], bool]:
    """The tables a CHECKED query names, CTE names left out, and whether
    that list is complete: False when a reference can't be read as one
    table (a wildcard ``events_*``, ``INFORMATION_SCHEMA``, a table
    function, a name with no dataset and no ``default_dataset``).
    ``default_dataset`` is ``(project or None, dataset)``, as
    :func:`~aisquare_pipe_bigquery.refs.parse_dataset` gives it."""
    try:
        (root,) = _parse(sql)
    except Exception:  # noqa: BLE001 — checked already; any surprise: unknown
        return [], False
    ctes = {
        cte.alias_or_name.lower() for cte in root.find_all(exp.CTE) if cte.alias_or_name
    }
    refs: list[TableRef] = []
    complete = True
    for node in root.find_all(exp.Table):
        name, dataset, project = node.name, node.db, node.catalog
        if not dataset and not project and name.lower() in ctes:
            continue
        if not dataset and default_dataset is not None:
            project, dataset = default_dataset[0] or "", default_dataset[1]
        if (
            not dataset
            or "INFORMATION_SCHEMA" in (name.upper(), dataset.upper())
            or not TABLE_ID.match(name)
            or not DATASET_ID.match(dataset)
            or (project and not PROJECT_ID.match(project))
        ):
            complete = False
            continue
        ref = TableRef(project or None, dataset, name)
        if ref not in refs:
            refs.append(ref)
    return refs, complete


__all__ = [
    "MAX_SQL_CHARS",
    "NotReadOnly",
    "TableRef",
    "check_select_only",
    "table_references",
]
