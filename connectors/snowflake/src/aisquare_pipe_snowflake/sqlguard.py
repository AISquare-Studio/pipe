"""SELECT-only SQL: two independent checks, both before any HTTP call.

1. **The lexer** (no parser): with string literals, quoted identifiers and
   ``--`` / ``//`` / ``/* */`` comments blanked, read once WITH and once
   WITHOUT backslash escapes (both readings must agree), the text is ONE
   statement (one trailing ``;`` is dropped) that starts with ``SELECT`` or
   ``WITH``, names none of the write, DDL, session or scripting keywords,
   and holds none of the Snowflake forms a read here never needs: ``$``
   (``$$`` strings, ``SYSTEM$`` functions, session variables, stage
   columns), ``@`` (stages), ``->`` / ``->>`` (the flow operator chains
   statements), ``|>``, backticks and backslashes. A table function
   (``TABLE(f(...))``, ``LATERAL f(...)``) must be one of the built-in
   read-only ones (:data:`TABLE_FUNCTIONS`): a stored procedure can be
   called that way, and it may write.
2. **The parser** (sqlglot, Snowflake dialect): exactly ONE statement whose
   root is a query (``SELECT``, ``WITH ... SELECT``, ``UNION`` /
   ``INTERSECT`` / ``EXCEPT`` / ``MINUS``, or a parenthesised one), with no
   write, DDL, session, transaction or unparsed node anywhere in its tree,
   and no table function outside :data:`TABLE_FUNCTIONS`. Text it can't
   parse is refused. Its "unsupported syntax" warning, which would log the
   SQL, is switched off.

Either one refusing refuses the statement (:class:`NotReadOnly`, with a
message naming why and never echoing the SQL). The parser understands the
dialect; the lexer catches what a parser might read more generously than
Snowflake does. Behind both, Snowflake is told to run ONE statement
(``MULTI_STATEMENT_COUNT`` 1), and the role holds read-only grants.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlglot import exp
from sqlglot.dialects.snowflake import Snowflake
from sqlglot.errors import SqlglotError

from aisquare_pipe_snowflake.refs import TableRef

MAX_SQL_CHARS = 20_000
#: Table functions a SELECT may call: built-in, read-only, row generators.
TABLE_FUNCTIONS = frozenset(
    {"FLATTEN", "SPLIT_TO_TABLE", "STRTOK_SPLIT_TO_TABLE", "GENERATOR"}
)

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
            "Show",
            "Put",
            "Get",
            "Placeholder",
            "Parameter",
            "SessionParameter",
        )
    )
    if isinstance(t, type)
)
# Where a FROM clause calls a function (``TABLE(f())``, ``LATERAL f()``).
_TABLE_FUNCTION_NODES: tuple[type[exp.Expression], ...] = tuple(
    t for t in (getattr(exp, "TableFromRows", None), exp.Lateral) if isinstance(t, type)
)
_WRITE_KEYWORDS = frozenset(
    {
        "ALTER",
        "ANALYZE",
        "BEGIN",
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
        "IMMEDIATE",
        "INSERT",
        "INTO",
        "LOAD",
        "MERGE",
        "NEXTVAL",
        "PROCEDURE",
        "PUT",
        "REMOVE",
        "RENAME",
        "RESET",
        "RESTORE",
        "REVOKE",
        "RM",
        "ROLLBACK",
        "SET",
        "SWAP",
        "TRANSACTION",
        "TRUNCATE",
        "UNDROP",
        "UNSET",
        "UPDATE",
        "UPSERT",
        "USE",
    }
)
_FIRST_KEYWORDS = frozenset({"SELECT", "WITH"})
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_QUOTES = "'\""
# Outside literals, identifiers and comments: forms no read here needs.
_FORBIDDEN = (
    ("$", "it uses $ ($$ strings, SYSTEM$ functions, variables or stage columns)"),
    ("@", "it names a stage (@)"),
    ("->", "it uses the -> / ->> operator"),
    ("|>", "it uses the |> operator"),
    ("`", "it holds a backtick"),
    ("\\", "it holds a backslash outside a string"),
)
_TABLE_CALL = re.compile(r"\bTABLE\s*\(", re.IGNORECASE)
_LATERAL = re.compile(r"\bLATERAL\b", re.IGNORECASE)
_CALLED = re.compile(
    r"\s*([A-Za-z_][A-Za-z0-9_]*(?:\s*\.\s*[A-Za-z_][A-Za-z0-9_]*)*)\s*\("
)


class NotReadOnly(ValueError):
    """The SQL is not one read-only query. The message says why."""


class _QuietParser(Snowflake.Parser):
    """sqlglot's Snowflake parser without its "contains unsupported syntax"
    warning, which logs the SQL text. The statement is refused anyway: an
    unsupported statement parses as a ``Command``."""

    def _warn_unsupported(self) -> None:
        return None


_DIALECT = Snowflake()


def check_select_only(sql: str) -> str:
    """The statement to send, if ``sql`` is ONE read-only query (both checks
    pass): stripped, without a single trailing ``;``. Else
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


def _lex(sql: str) -> _Lexed:
    if any(not (ch.isprintable() or ch in " \t\r\n") for ch in sql):
        return _Lexed(sql, "it holds a control character")
    statements: set[str] = set()
    for backslash in (True, False):
        blanked = _blank(sql, backslash_escapes=backslash)
        if blanked is None:
            return _Lexed(sql, "it has an unterminated string, identifier or comment")
        problem, cut = _read(blanked)
        if problem is not None:
            return _Lexed(sql, problem)
        statements.add(sql[:cut].strip())
    if len(statements) != 1:
        return _Lexed(sql, "its quoting reads two ways")
    (statement,) = statements
    return _Lexed(statement, None)


def _read(text: str) -> tuple[str | None, int]:
    """Problems in blanked ``text``, and where the statement ends (before
    one trailing ``;``)."""
    for token, why in _FORBIDDEN:
        if token in text:
            return why, len(text)
    cut = len(text)
    separators = [i for i, ch in enumerate(text) if ch == ";"]
    if separators:
        last = separators[-1]
        if len(separators) > 1 or text[last + 1 :].strip():
            return "it is more than one statement", cut
        cut = last
    body = text[:cut]
    if not body.strip():
        return "it is empty", cut
    words = [w.upper() for w in _WORD.findall(body)]
    if not words or words[0] not in _FIRST_KEYWORDS:
        return "it does not start with SELECT or WITH", cut
    written = sorted(set(words) & _WRITE_KEYWORDS)
    if written:
        return f"it names {written[0]}", cut
    for match in _TABLE_CALL.finditer(body):
        called = _CALLED.match(body, match.end())
        if called is None or _last_name(called.group(1)) not in TABLE_FUNCTIONS:
            return _table_function_problem(), cut
    for match in _LATERAL.finditer(body):
        rest = body[match.end() :].lstrip()
        if rest.startswith("("):
            continue  # a lateral subquery
        called = _CALLED.match(body, match.end())
        if called is None or _last_name(called.group(1)) not in TABLE_FUNCTIONS:
            return _table_function_problem(), cut
    return None, cut


def _last_name(dotted: str) -> str:
    return dotted.replace(" ", "").split(".")[-1].upper()


def _table_function_problem() -> str:
    return "it calls a table function other than " + ", ".join(sorted(TABLE_FUNCTIONS))


def _blank(sql: str, *, backslash_escapes: bool) -> str | None:
    """``sql`` with quoted text and ``--`` / ``//`` / ``/* */`` comments
    turned into spaces, same length (so positions match); None when a quote
    or a block comment never closes."""
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
            j = min(j, n)
            out.append(" " * (j - i))
            i = j
        elif sql.startswith("--", i) or sql.startswith("//", i):
            end = sql.find("\n", i)
            end = n if end < 0 else end
            out.append(" " * (end - i))
            i = end
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end < 0:
                return None
            out.append(" " * (end + 2 - i))
            i = end + 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


# -- the parser ----------------------------------------------------------------------


def _parse(sql: str) -> list[exp.Expression]:
    """Every statement in ``sql`` (sqlglot, Snowflake dialect, silently)."""
    tokens = _DIALECT.tokenize(sql)
    parser = _QuietParser(dialect=_DIALECT)
    return [s for s in parser.parse(tokens, sql) if s is not None]


def _parsed_problem(sql: str) -> str | None:
    try:
        statements = _parse(sql)
    except SqlglotError:
        return "it does not parse as Snowflake SQL"
    except Exception:  # noqa: BLE001 — a parser bug refuses, never admits
        return "it does not parse as Snowflake SQL"
    if len(statements) != 1:
        return "it is more than one statement" if statements else "it is empty"
    (root,) = statements
    if not isinstance(root, _QUERY_ROOTS):
        return f"it is a {root.key.upper()} statement, not a query"
    for node in root.walk():
        if isinstance(node, _WRITE_NODES):
            return f"it contains {node.key.upper()}"
        if isinstance(node, _TABLE_FUNCTION_NODES) and _calls_unlisted(node.this):
            return _table_function_problem()
        if (
            isinstance(node, exp.Table)
            and isinstance(node.this, exp.Func)
            and _calls_unlisted(node.this)
        ):
            return _table_function_problem()
    return None


def _calls_unlisted(called: object) -> bool:
    """Whether a ``TABLE(...)`` / ``LATERAL ...`` calls anything but a
    function in :data:`TABLE_FUNCTIONS` (FLATTEN parses as ``Explode``)."""
    if isinstance(called, (exp.Subquery, exp.Select)):
        return False
    if isinstance(called, exp.Explode):
        return False
    if isinstance(called, exp.Func) and not isinstance(called, exp.Anonymous):
        return called.sql_name().upper() not in TABLE_FUNCTIONS
    if isinstance(called, exp.Anonymous):
        return str(called.name).upper() not in TABLE_FUNCTIONS
    return True


# -- the tables a query reads --------------------------------------------------------


def _resolved(identifier: object) -> str | None:
    if not isinstance(identifier, exp.Identifier):
        return None
    name = str(identifier.this)
    return name if identifier.quoted else name.upper()


def table_references(
    sql: str, *, database: str | None, schema: str | None
) -> tuple[list[TableRef], bool]:
    """The tables a CHECKED query names (CTE names left out), resolved with
    the default ``database``/``schema``, and whether that list is complete:
    False when a reference can't be read as one table (a table function,
    ``IDENTIFIER(...)``, ``INFORMATION_SCHEMA``, a name without a database or
    schema and no default)."""
    try:
        (root,) = _parse(sql)
    except Exception:  # noqa: BLE001 — checked already; any surprise: unknown
        return [], False
    ctes = {
        cte.alias_or_name.upper() for cte in root.find_all(exp.CTE) if cte.alias_or_name
    }
    refs: list[TableRef] = []
    complete = True
    for node in root.find_all(exp.Table):
        name = _resolved(node.this)
        db_ident, schema_ident = node.args.get("catalog"), node.args.get("db")
        if name is None:
            complete = False
            continue
        if db_ident is None and schema_ident is None and name.upper() in ctes:
            continue
        db = _resolved(db_ident) if db_ident is not None else database
        sch = _resolved(schema_ident) if schema_ident is not None else schema
        if db_ident is None and schema_ident is not None:
            db = database
        if not db or not sch or "INFORMATION_SCHEMA" in (sch.upper(), name.upper()):
            complete = False
            continue
        ref = TableRef(db, sch, name)
        if ref not in refs:
            refs.append(ref)
    if any(isinstance(n, _TABLE_FUNCTION_NODES) for n in root.walk()):
        complete = False
    return refs, complete


__all__ = [
    "MAX_SQL_CHARS",
    "TABLE_FUNCTIONS",
    "NotReadOnly",
    "check_select_only",
    "table_references",
]
