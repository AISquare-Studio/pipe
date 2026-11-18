"""SELECT-only SQL for Databricks: two independent checks, both before any HTTP call.

Either one refusing refuses the statement (:class:`NotReadOnly`, whose
message says why and never echoes the SQL), and each one refuses every
write on its own: neither is a backup for the other.

1. **The lexer** (no parser) reads the text as Spark's own lexer does
   (``SqlBaseLexer.g4``), and refuses wherever two readers could disagree on
   where a token ends, rather than pick one:

   - a ``--`` comment ends at the first CR or LF. Spark carries it on past a
     backslash right before the LF (``SIMPLE_COMMENT: '--' ('\\\\\\n' |
     ~[\\r\\n])*``) and sqlglot does not, so a backslash right before the
     line break is refused; so is a comment a bare CR ends (a CR not
     followed by LF), which some readers don't take as a line end;
   - ``/* */`` comments nest in Spark (``NESTED_BRACKETED_COMMENT``) and in
     some readers only: a ``/*`` inside one is refused, as is one that never
     closes. ``/*+`` opens a hint, whose contents Spark reads as tokens, and
     so does this lexer; ``*/`` outside a comment is one token, as in Spark;
   - quotes: Spark takes backslash escapes in ``'...'`` and ``"..."`` but
     never in backtick identifiers, and none in ``r'...'`` raw strings. Every
     quoted token must end at the same place whether a backslash escapes the
     next character or not; if not, it is refused (write a quote in a string
     as ``''``, or inside the other quote kind). So every reading (Spark's,
     sqlglot's, with or without escapes or a raw prefix) ends every quoted
     token at the same place;
   - ``${`` anywhere is refused: Spark substitutes ``${...}`` variables
     before it reads the text, inside strings and comments too;
   - outside quotes and comments only ASCII is allowed (Spark 3's lexer
     upper-cases with ``Character.toUpperCase``, so ``ınsert`` reads as
     ``INSERT``), and no backslash; anywhere, no control character but tab,
     LF and CR, and no NEL, LS or PS.

   Then the text must be ONE statement (trailing ``;`` allowed) that starts
   with ``SELECT`` or ``WITH`` and names none of the write, DDL, session or
   scripting words (``INSERT``, ``UPDATE``, ``DELETE``, ``MERGE``,
   ``CREATE``, ``DROP``, ``ALTER``, ``SET``, ``USE``, ``COPY``, ``INTO``,
   ``EXECUTE``, ...), calls none of the side-effect functions
   (:data:`SIDE_EFFECT_FUNCTIONS`: ``http_request``, ``remote_query``,
   ``reflect``, ``java_method``, ``secret``, ``IDENTIFIER(...)``, and any
   ``ai_*``), plainly or as a quoted name, and has no ``USING '<script>'``
   (``SELECT TRANSFORM(...) USING 'cmd'`` runs a command).

2. **The parser** (sqlglot, Databricks dialect): the text parses into exactly
   ONE statement whose root is a query (``SELECT``, ``WITH ... SELECT``, a
   ``UNION``/``INTERSECT``/``EXCEPT`` of queries, or a parenthesised one),
   and no node anywhere in its tree is a write, DDL, session, transaction or
   script-transform node (``INSERT`` ... ``SELECT ... INTO``, ``SET``,
   ``USE``, ``COPY``, ``TRANSFORM ... USING``, an unparsed ``Command``, ...)
   or a call of a side-effect function. Text it can't parse is refused.

   sqlglot logs pieces of the SQL while it parses ("Invalid JSON path
   syntax. ... <the path>" at WARNING for a JSON key with a dash, in
   statements this guard admits). So it parses inside
   :func:`aisquare.pipe.core.sql.quiet_sqlglot`, the helper every SQL guard
   shares: nothing sqlglot logs then gets out, in this context only.
   A sqlglot older than the extra's floor (:data:`SQLGLOT_FLOOR`: 25.0 to
   25.2 don't nest Spark's block comments; another package can bring an old
   one in) refuses every statement.

Behind these, Databricks itself: the principal should hold only ``USE`` and
``SELECT`` grants (see the README), so a write that got past both checks
would still be refused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
from aisquare.pipe.core.sql import quiet_sqlglot, sqlglot_at_least
from sqlglot import exp
from sqlglot.errors import SqlglotError

MAX_SQL_CHARS = 20_000
#: The oldest sqlglot the guard runs on: the ``[databricks]`` extra's floor.
SQLGLOT_FLOOR = (26, 25)

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
            "Declare",
            "DeclareItem",
            "Use",
            "Transaction",
            "Commit",
            "Rollback",
            "Copy",
            "LoadData",
            "Export",
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
            # SELECT TRANSFORM(...) USING 'script': runs a command.
            "QueryTransform",
        )
    )
    if isinstance(t, type)
)

#: Functions a read-only query has no use for, refused when called (by name,
#: qualified or quoted): an HTTP request or a query in another database
#: through a Unity Catalog connection (either can write there), a Java
#: method by reflection, a secret's value, and a name built from a string
#: (``IDENTIFIER('...')``, which the checks can't read through). Any
#: ``ai_*`` call (model serving, billed outside the warehouse) is refused too.
SIDE_EFFECT_FUNCTIONS = frozenset(
    {
        "HTTP_REQUEST",
        "REMOTE_QUERY",
        "REFLECT",
        "JAVA_METHOD",
        "TRY_REFLECT",
        "SECRET",
        "TRY_SECRET",
        "IDENTIFIER",
    }
)
_SIDE_EFFECT_PREFIX = "AI_"

#: Words no read-only Databricks query has outside a string, identifier or
#: comment: statements that write, change the session or a cache, run a
#: script or procedure, and ``INTO``. A column with such a name is written in
#: backticks.
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
        "RECORDREADER",
        "RECORDWRITER",
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
#: A word as Spark's IDENTIFIER rule reads one (letters, digits, ``_``),
#: started at a letter or ``_``: ``1INSERT`` gives ``INSERT`` (Spark reads one
#: identifier there; refusing is the safe side).
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_QUOTES = "'\"`"
_LINE_END = re.compile(r"[\r\n]")
#: Control characters a statement may hold.
_ALLOWED_CONTROLS = frozenset("\t\n\r")
#: Line ends some readers know and Spark doesn't: NEL, LS, PS.
_ODD_LINE_ENDS = frozenset("\x85  ")


class NotReadOnly(ValueError):
    """The SQL is not one read-only query. The message says why."""


def check_select_only(sql: str) -> str:
    """``sql`` stripped, if it is ONE read-only query (both checks pass);
    else :class:`NotReadOnly`."""
    if not sqlglot_at_least(str(getattr(sqlglot, "__version__", "")), SQLGLOT_FLOOR):
        raise NotReadOnly(
            "the SQL guard needs sqlglot "
            + ".".join(map(str, SQLGLOT_FLOOR))
            + " or later and this host has an older one, so no statement may run; "
            "the host must install aisquare-pipe[databricks] as declared"
        )
    if not isinstance(sql, str) or not sql.strip():
        raise NotReadOnly("the statement is empty")
    if len(sql) > MAX_SQL_CHARS:
        raise NotReadOnly(f"the statement is longer than {MAX_SQL_CHARS} characters")
    problem = _lexical(sql) or _parsed(sql)
    if problem is not None:
        raise NotReadOnly(f"only one SELECT (or WITH ... SELECT) may run: {problem}")
    return sql.strip()


def _side_effect(name: str) -> bool:
    upper = name.upper()
    return upper in SIDE_EFFECT_FUNCTIONS or upper.startswith(_SIDE_EFFECT_PREFIX)


# -- the parser ----------------------------------------------------------------------


def _parse(sql: str) -> list[exp.Expression]:
    """Every statement in ``sql`` (sqlglot, Databricks dialect, silently:
    nothing sqlglot logs while it parses gets out)."""
    with quiet_sqlglot():
        return [s for s in sqlglot.parse(sql, read="databricks") if s is not None]


def _function_name(node: exp.Expression) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name)
    if isinstance(node, exp.Func):
        return node.sql_name()
    return ""


def _parsed(sql: str) -> str | None:
    try:
        statements = _parse(sql)
    except SqlglotError:
        return "it does not parse as Databricks SQL"
    except Exception:  # noqa: BLE001 — a parser bug refuses, never admits
        return "it does not parse as Databricks SQL"
    if len(statements) != 1:
        return "it is more than one statement" if statements else "it is empty"
    (root,) = statements
    if not isinstance(root, _QUERY_ROOTS):
        return f"it is a {root.key.upper()} statement, not a query"
    for node in root.walk():
        if isinstance(node, _WRITE_NODES):
            return f"it contains {node.key.upper()}"
        name = _function_name(node)
        if name and _side_effect(name.rsplit(".", 1)[-1]):
            return f"it calls {name.upper()}"
    return None


# -- the lexer -----------------------------------------------------------------------


class _Unreadable(Exception):
    """Text the lexer won't vouch for: Spark may read it some other way than
    another reader, or not at all. The message says why and never echoes
    the SQL."""


@dataclass(frozen=True)
class _Quoted:
    start: int  # the opening quote
    end: int  # just after the closing quote
    name: str  # the text inside, a doubled quote read as one


def _lexical(sql: str) -> str | None:
    try:
        text, separators, quoted = _blank(sql)
    except _Unreadable as unreadable:
        return str(unreadable)
    if separators and text[separators[0] + 1 :].replace(";", " ").strip():
        return "it is more than one statement"
    words = list(_WORD.finditer(text))
    if not words:
        return "it is empty"
    if words[0].group().upper() not in _FIRST_KEYWORDS:
        return "it does not start with SELECT or WITH"
    written = sorted({w.group().upper() for w in words} & _WRITE_KEYWORDS)
    if written:
        return (
            f"it names {written[0]} (write a column or table with that name in "
            "backticks)"
        )
    for word in words:
        if _side_effect(word.group()) and _calls(text, word.end()):
            return f"it calls {word.group().upper()}"
        if word.group().upper() == "USING" and _script_follows(text, word.end()):
            return "it runs a script (TRANSFORM ... USING '...')"
    for name in quoted:
        # A `name` or "name" (an identifier under ANSI double quotes) called.
        if (
            text[name.start] != "'"
            and _side_effect(name.name.rsplit(".", 1)[-1])
            and _calls(text, name.end)
        ):
            return f"it calls {name.name.upper()}"
    return None


def _calls(text: str, at: int) -> bool:
    """Whether the next thing after ``at`` (spaces and comments are already
    blanked) is an open parenthesis: the name before it is called."""
    rest = text[at:].lstrip()
    return rest.startswith("(")


def _script_follows(text: str, at: int) -> bool:
    """Whether a string (``'...'``, ``"..."``, ``r'...'``) comes next."""
    rest = text[at:].lstrip()
    if rest[:1] in ("r", "R"):
        rest = rest[1:]
    return rest[:1] in ("'", '"')


def _quoted_end(sql: str, start: int, *, backslash_escapes: bool) -> int | None:
    """Where the token opened at ``start`` ends (the index after its closing
    quote), reading a doubled quote as an escaped one and, when
    ``backslash_escapes``, a backslash as escaping the next character. None
    when it never closes."""
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
    return None


def _blank(sql: str) -> tuple[str, list[int], list[_Quoted]]:
    """``sql`` with comments and the inside of every quoted token replaced
    by spaces (same length; a quoted token keeps its opening quote, so what
    follows a word can still be seen to be a string), the positions of the
    ``;`` outside them, and the quoted tokens. :class:`_Unreadable` for
    anything the module docstring lists as refused."""
    for ch in sql:
        if (ch < " " and ch not in _ALLOWED_CONTROLS) or ch == "\x7f":
            raise _Unreadable("it has a control character")
        if ch in _ODD_LINE_ENDS or "\x80" <= ch <= "\x9f":
            raise _Unreadable("it has a NEL, LS, PS or C1 control character")
    if "${" in sql:
        raise _Unreadable(
            "it has ${...}, which Spark substitutes before it reads the SQL"
        )
    out = list(sql)
    separators: list[int] = []
    quoted: list[_Quoted] = []
    i, n = 0, len(sql)

    def blank(start: int, end: int) -> None:
        for k in range(start, end):
            out[k] = " "

    while i < n:
        ch = sql[i]
        if ch in _QUOTES:
            plain = _quoted_end(sql, i, backslash_escapes=False)
            escaped = _quoted_end(sql, i, backslash_escapes=True)
            if plain is None and escaped is None:
                raise _Unreadable("it has an unterminated string or quoted identifier")
            if plain is None or plain != escaped:
                raise _Unreadable(
                    "a backslash in a string or quoted identifier makes where it ends "
                    "depend on the reader (write a quote inside a string as '')"
                )
            quoted.append(_Quoted(i, plain, sql[i + 1 : plain - 1].replace(ch * 2, ch)))
            blank(i + 1, plain)
            i = plain
        elif sql.startswith("--", i):
            line_end = _LINE_END.search(sql, i + 2)
            end = n if line_end is None else line_end.start()
            if line_end is not None:
                if sql[end - 1] == "\\" and end - 1 >= i + 2:
                    raise _Unreadable(
                        "a -- comment has a backslash before its line break (Spark "
                        "reads the next line as part of the comment, other readers don't)"
                    )
                if sql[end] == "\r" and not sql.startswith("\r\n", end):
                    raise _Unreadable(
                        "a -- comment ends at a bare CR (readers disagree on that line end)"
                    )
            blank(i, end)
            i = end
        elif sql.startswith("/*+", i):
            # A hint: Spark reads what is inside as tokens, and so does this.
            i += 3
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
        elif sql.startswith("*/", i):
            # One token in Spark (a hint's end), so its '/' opens no comment.
            i += 2
        elif ch == "\\":
            raise _Unreadable("it has a backslash outside quotes")
        elif ch > "\x7f":
            raise _Unreadable(
                "it has a non-ASCII character outside quotes and comments (write such "
                "a name in backticks)"
            )
        else:
            if ch == ";":
                separators.append(i)
            i += 1
    return "".join(out), separators, quoted


__all__ = [
    "MAX_SQL_CHARS",
    "SIDE_EFFECT_FUNCTIONS",
    "SQLGLOT_FLOOR",
    "NotReadOnly",
    "check_select_only",
]
