"""SELECT-only SQL: two checks, both before any HTTP call.

1. **The lexer** (no parser) blanks string literals, quoted identifiers and
   ``--`` / ``//`` / ``/* */`` comments. Snowflake reads a backslash as an
   escape in a ``'...'`` string and never in a ``"..."`` identifier, and the
   lexer reads the text that way first (sqlglot's tokenizer agrees). It then
   reads it the three other ways a reader could (escapes in both, in
   neither, in ``"..."`` only), and all four must agree on where every
   quote, comment and the statement end. Otherwise the text is refused, so a
   quote inside a string is written ``''``, never ``\\'``. In Snowflake's
   reading the text must be ONE statement (one trailing ``;`` is dropped)
   that starts with ``SELECT`` or ``WITH``.
   - It names none of the write, DDL, session or scripting keywords.
   - It holds none of the forms a read here never needs: ``$`` (``$$``
     strings, ``SYSTEM$`` functions, session variables, stage columns),
     ``@`` (stages), ``->`` / ``->>`` (the flow operator chains
     statements), ``|>``, a ``!`` other than ``!=`` (a model's method),
     backticks and backslashes.
   - No ``"..."`` name holds a ``$`` or is ``NEXTVAL`` (``s."NEXTVAL"``
     still takes a sequence's next value).
   - It calls no Cortex AI function (``SNOWFLAKE.CORTEX.*``: no ``CORTEX``
     qualifier at all, and no ``AI_*`` call, quoted or not). They bill AI
     credits, which no warehouse limit bounds. ``IDENTIFIER(...)`` is
     refused too, since it turns a string, which neither check reads, into
     a name.
   - A table function (``TABLE(f(...))``, ``LATERAL f(...)``) must be one
     of the built-in read-only ones (:data:`TABLE_FUNCTIONS`), unqualified
     (``s.FLATTEN`` is a user-defined function). A stored procedure can be
     called that way, and it may write. A qualified call named like one of
     them (``s.FLATTEN(...)``) is refused wherever it stands.
   - A ``--`` or ``//`` comment ends at the first CR or LF, and one a bare
     CR ends (no LF after it) is refused. So is a ``/*`` inside a
     ``/* */`` comment. Readers disagree on both (sqlglot ends a line at a
     CR; sqlglot 25.3 to 26.24 nested BigQuery's comments), and a
     disagreement can hide a statement from one check.
2. **The parser** reads the text with sqlglot (Snowflake dialect), tokens
   and tree. The text must be exactly ONE statement whose root is a query
   (``SELECT``, ``WITH ... SELECT``, ``UNION`` / ``INTERSECT`` / ``EXCEPT``
   / ``MINUS``, or a parenthesised one). Anywhere in it, it may hold none of
   the following:
   - a write, DDL, session, transaction, bind-variable or unparsed node;
   - a ``$`` or ``@`` token (variables, stage columns, stages), a ``$$``
     string, a name holding a ``$`` (identifiers quoted or not, words,
     function names), or a table named ``@...``;
   - a ``NEXTVAL`` name (quoted or not);
   - a Cortex AI call (an ``AI_*`` function, a ``CORTEX`` qualifier), an
     ``IDENTIFIER(...)``, or a model's method (``m!PREDICT(...)``);
   - a table function outside :data:`TABLE_FUNCTIONS`, including a
     qualified one, in ``TABLE(...)``, ``LATERAL`` or a bare ``FROM`` /
     ``JOIN``.
   Text it can't parse is refused. sqlglot logs pieces of the SQL (its
   "unsupported syntax" warning; "Invalid JSON path syntax" with the path,
   for any JSON key with a dash), so nothing it logs while this module
   parses gets out: a filter on the "sqlglot" logger drops those records,
   in this context only. Nothing is disabled globally.

Either one refusing refuses the statement (:class:`NotReadOnly`, with a
message naming why and never echoing the SQL). The two read the text
separately, and most forms above are refused by both, each on its own. The
few that only one of them refuses:
- Only the lexer: a write keyword as a bare word (``SELECT update FROM t``,
  where sqlglot reads a column), ``|>``, a control character, a second
  ``;``, and the text readers are known to disagree on (a backslash before a
  quote, a bare CR, nested comments). The parser is only as faithful as
  sqlglot's reading of Snowflake, so the lexer refuses such text.
- Only the parser: a table function in a bare ``FROM`` or ``JOIN`` under a
  name other than the built-ins' (``FROM t, my_udtf(1)``), which the lexer
  can't tell from ``EXTRACT(YEAR FROM TO_DATE(x))``, and the ``?`` and
  ``:1`` binds.
- Neither looks inside a string: what it holds is data.
Behind both, Snowflake is told to run ONE statement
(``MULTI_STATEMENT_COUNT`` 1), and the role holds read-only grants.

:func:`check_context` refuses a default schema named ``CORTEX``: there, an
unqualified ``COMPLETE(...)`` is ``SNOWFLAKE.CORTEX.COMPLETE``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from sqlglot import exp
from sqlglot.dialects.snowflake import Snowflake
from sqlglot.errors import SqlglotError
from sqlglot.tokens import Token, TokenType

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
# Nodes carrying a name the query uses: a column, table or alias (quoted or
# not), a bare word, a function call.
_NAMED_NODES: tuple[type[exp.Expression], ...] = (
    exp.Identifier,
    exp.Var,
    exp.Anonymous,
)
# Where a FROM clause calls a function (``TABLE(f())``, ``LATERAL f()``).
_TABLE_FUNCTION_NODES: tuple[type[exp.Expression], ...] = tuple(
    t for t in (getattr(exp, "TableFromRows", None), exp.Lateral) if isinstance(t, type)
)
# ``$$...$$`` (and a tagged dollar quote), as a node and as a token.
_DOLLAR_QUOTED_NODES: tuple[type[exp.Expression], ...] = tuple(
    t
    for t in (getattr(exp, "RawString", None), getattr(exp, "Heredoc", None))
    if isinstance(t, type)
)
_DOLLAR_QUOTED_TOKENS = frozenset(
    t
    for t in (
        getattr(TokenType, "RAW_STRING", None),
        getattr(TokenType, "HEREDOC_STRING", None),
    )
    if t is not None
)
# A model's method (``m!PREDICT(...)``; sqlglot 26 can't parse it at all).
_MODEL_METHOD_NODES: tuple[type[exp.Expression], ...] = tuple(
    t for t in (getattr(exp, "ModelAttribute", None),) if isinstance(t, type)
)
# Tokens whose text is a literal's value (data, not a name).
_STRING_TOKENS = frozenset(
    t
    for t in (
        getattr(TokenType, name, None)
        for name in (
            "STRING",
            "NATIONAL_STRING",
            "HEX_STRING",
            "BIT_STRING",
            "BYTE_STRING",
            "UNICODE_STRING",
        )
    )
    if t is not None
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
# Refused as a name even in double quotes (and by the parser, as any
# identifier): ``s."NEXTVAL"`` still takes a sequence's next value.
_NEVER_NAMED = frozenset({"NEXTVAL"})
_FIRST_KEYWORDS = frozenset({"SELECT", "WITH"})
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_QUOTES = "'\""
_LINE_END = re.compile(r"[\r\n]")
# How a backslash reads in quoted text: (in '...' strings, in "..."
# identifiers). Snowflake's own reading comes first: an escape in a string,
# never in an identifier (sqlglot's tokenizer reads it the same way). The
# other three are there so that text whose quoting reads more than one way
# is refused, whatever a reader makes of a backslash.
_READINGS = ((True, False), (True, True), (False, False), (False, True))
_MORE_THAN_ONE_WAY = (
    "its quoting reads more than one way (a backslash before a quote; write a "
    "quote inside a string as '')"
)
_DOLLAR = "it uses $ ($$ strings, SYSTEM$ functions, variables or stage columns)"
_DOLLAR_NAME = "it names something holding $ (SYSTEM$ functions, variables)"
_DOLLAR_STRING = "it holds a $$ string"
_STAGE = "it names a stage (@)"
_PARAMETER = "it uses $ or @ (variables, stage columns or stages)"
_CORTEX = (
    "it calls a Cortex AI function (SNOWFLAKE.CORTEX.* or AI_*), which bills AI "
    "credits outside the warehouse's limits"
)
_IDENTIFIER = "it uses IDENTIFIER(...), a name written as a string"
_MODEL_METHOD = "it calls a model's method (!)"
# Outside literals, identifiers and comments: forms no read here needs.
_FORBIDDEN = (
    ("$", _DOLLAR),
    ("@", _STAGE),
    ("->", "it uses the -> / ->> operator"),
    ("|>", "it uses the |> operator"),
    ("`", "it holds a backtick"),
    ("\\", "it holds a backslash outside a string"),
)
# ``m!PREDICT(...)`` calls a model's method; ``!=`` is "not equal".
_BANG = re.compile(r"!(?!=)")
# What a TABLE( or a LATERAL calls.
_CALLED = re.compile(
    r"\s*([A-Za-z_][A-Za-z0-9_]*(?:\s*\.\s*[A-Za-z_][A-Za-z0-9_]*)*)\s*\("
)
# A qualified call named like a built-in table function (``s.FLATTEN(``: a
# user-defined function, wherever it stands).
_QUALIFIED_TABLE_FUNCTION = re.compile(
    r"\.\s*(?:" + "|".join(sorted(TABLE_FUNCTIONS)) + r")\s*\(", re.IGNORECASE
)
# What follows a quoted name: the next character that isn't blank.
_NEXT = re.compile(r"\s*(\S?)")
# Qualifiers no read here needs: SNOWFLAKE.CORTEX holds the Cortex AI
# functions (COMPLETE, SUMMARIZE, EMBED_TEXT_768, ...).
_REFUSED_QUALIFIERS = frozenset({"CORTEX"})


class NotReadOnly(ValueError):
    """The SQL is not one read-only query. The message says why."""


class _Unreadable(Exception):
    """The lexer can't read the text one way (the message says why)."""


def _called_problem(name: str) -> str | None:
    """Why a function called ``name`` (as written, quoted or not) is refused,
    in both checks: an ``AI_*`` Cortex AI function, or ``IDENTIFIER``."""
    upper = name.upper()
    if upper.startswith("AI_"):
        return _CORTEX
    if upper == "IDENTIFIER":
        return _IDENTIFIER
    return None


def _qualifier_problem(name: str) -> str | None:
    """Why ``name`` (as written) is refused as a qualifier (``CORTEX.f``)."""
    if name.upper() in _REFUSED_QUALIFIERS:
        return _CORTEX
    return None


class _QuietParser(Snowflake.Parser):
    """sqlglot's Snowflake parser without its "contains unsupported syntax"
    warning, which logs the SQL text. The statement is refused anyway: an
    unsupported statement parses as a ``Command``."""

    def _warn_unsupported(self) -> None:
        return None


_DIALECT = Snowflake()

# sqlglot logs pieces of the SQL it parses: "Invalid JSON path syntax. ...
# <the path>" for any JSON key with a dash (``GET_PATH(v, 'a-b')``,
# ``v:'x-y'``), at WARNING, for statements this guard admits. Every sqlglot
# module logs through the one "sqlglot" logger. This filter drops its records
# while THIS module parses, in this context only (a ContextVar, so another
# thread or task logging through sqlglot meanwhile is untouched). Nothing is
# disabled globally.
_SQLGLOT_LOGGER = logging.getLogger("sqlglot")
_PARSING: ContextVar[bool] = ContextVar(
    "aisquare_pipe_snowflake_sqlguard_parsing", default=False
)


class _DropWhileParsing(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _PARSING.get()


_QUIET_WHILE_PARSING = _DropWhileParsing()


@contextmanager
def _sqlglot_silenced() -> Iterator[None]:
    # Re-attached on every parse: a host that resets the logger's filters
    # doesn't bring the leak back.
    if _QUIET_WHILE_PARSING not in _SQLGLOT_LOGGER.filters:
        _SQLGLOT_LOGGER.addFilter(_QUIET_WHILE_PARSING)
    token = _PARSING.set(True)
    try:
        yield
    finally:
        _PARSING.reset(token)


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


def check_context(database: str | None, schema: str | None) -> None:
    """:class:`NotReadOnly` when a checked query would run with the default
    schema ``CORTEX`` (any database, any case). There an unqualified
    ``COMPLETE(...)`` is ``SNOWFLAKE.CORTEX.COMPLETE``, a Cortex AI function
    that :func:`check_select_only` can't see as one. ``database`` is not
    looked at."""
    del database  # any database: the account's own CORTEX schemas fail closed too
    if schema is not None and schema.upper() in _REFUSED_QUALIFIERS:
        raise NotReadOnly(
            "the default schema may not be CORTEX: an unqualified function there is a "
            "Cortex AI function (SNOWFLAKE.CORTEX.*), which bills AI credits outside "
            "the warehouse's limits"
        )


# -- the lexer -----------------------------------------------------------------------


@dataclass(frozen=True)
class _Lexed:
    statement: str
    problem: str | None


@dataclass(frozen=True)
class _Quoted:
    """A ``"..."`` identifier: its name (``""`` read as ``"``) and where its
    closing quote ends."""

    name: str
    end: int


def _lex(sql: str) -> _Lexed:
    """Snowflake's reading of ``sql`` (:data:`_READINGS`' first) decides,
    and reports its own problem. Any other reading that finds a problem, or
    ends the statement elsewhere, refuses the text as reading more than one
    way."""
    if any(not (ch.isprintable() or ch in " \t\r\n") for ch in sql):
        return _Lexed(sql, "it holds a control character")
    statement = ""
    for index, (in_strings, in_names) in enumerate(_READINGS):
        quoted: list[_Quoted] = []
        problem: str | None
        try:
            blanked = _blank(
                sql,
                escapes_in_strings=in_strings,
                escapes_in_names=in_names,
                quoted=quoted,
            )
        except _Unreadable as unreadable:
            problem, cut = str(unreadable), len(sql)
        else:
            problem, cut = _read(blanked)
            if problem is None:
                problem = _quoted_name_problem(quoted, blanked)
        if index == 0:
            if problem is not None:
                return _Lexed(sql, problem)
            statement = sql[:cut].strip()
        elif problem is not None or sql[:cut].strip() != statement:
            return _Lexed(sql, _MORE_THAN_ONE_WAY)
    return _Lexed(statement, None)


def _read(text: str) -> tuple[str | None, int]:
    """Problems in blanked ``text``, and where the statement ends (before
    one trailing ``;``)."""
    for token, why in _FORBIDDEN:
        if token in text:
            return why, len(text)
    if _BANG.search(text):
        return _MODEL_METHOD, len(text)
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
    if _QUALIFIED_TABLE_FUNCTION.search(body):
        return _table_function_problem(), cut
    # Word by word, as Snowflake splits them: a name starts with a letter or
    # an underscore, so ``1AI_C(1)`` is the number 1, then a call to AI_C (a
    # word boundary, ``\b``, would see one word there and miss the call).
    for word in _WORD.finditer(body):
        name = word.group()
        following = _NEXT.match(body, word.end())
        after = following.group(1) if following else ""
        problem = None
        if after == "(":
            problem = _called_problem(name)
        elif after == ".":
            problem = _qualifier_problem(name)
        if problem is not None:
            return problem, cut
        upper = name.upper()
        if upper == "TABLE" and after == "(" and following is not None:
            called = _CALLED.match(body, following.end())
            if called is None or not _is_listed(called.group(1)):
                return _table_function_problem(), cut
        elif upper == "LATERAL" and after != "(":  # LATERAL (...) is a subquery
            called = _CALLED.match(body, word.end())
            if called is None or not _is_listed(called.group(1)):
                return _table_function_problem(), cut
    return None, cut


def _is_listed(called: str) -> bool:
    """Whether a called name is one of :data:`TABLE_FUNCTIONS`, UNQUALIFIED:
    ``s.FLATTEN`` or ``db.s.GENERATOR`` names a user-defined function."""
    return "." not in called and called.upper() in TABLE_FUNCTIONS


def _quoted_name_problem(quoted: list[_Quoted], blanked: str) -> str | None:
    """What the word checks can't see in a ``"..."`` identifier:
    ``s."NEXTVAL"`` takes a sequence's next value as surely as ``s.NEXTVAL``
    does, ``"SYSTEM$ABORT_SESSION"(...)`` and ``"AI_COMPLETE"(...)`` call
    the functions, ``"CORTEX".f(...)`` qualifies one, and
    ``s."FLATTEN"(...)`` calls a user-defined one."""
    for name in quoted:
        upper = name.name.upper()
        if upper in _NEVER_NAMED:
            return f"it names {upper}"
        if "$" in name.name:
            return _DOLLAR_NAME
        following = _NEXT.match(blanked, name.end)
        after = following.group(1) if following else ""
        if after == "(":
            problem = _called_problem(name.name)
            if problem is not None:
                return problem
            if upper in TABLE_FUNCTIONS:
                return _table_function_problem()
        elif after == ".":
            problem = _qualifier_problem(name.name)
            if problem is not None:
                return problem
    return None


def _table_function_problem() -> str:
    return (
        "it calls a table function other than "
        + ", ".join(sorted(TABLE_FUNCTIONS))
        + " (unqualified)"
    )


def _blank(
    sql: str,
    *,
    escapes_in_strings: bool,
    escapes_in_names: bool,
    quoted: list[_Quoted] | None = None,
) -> str:
    """``sql`` with quoted text and ``--`` / ``//`` / ``/* */`` comments
    turned into spaces, same length (so positions match). A backslash
    escapes the next character in a ``'...'`` string when
    ``escapes_in_strings``, in a ``"..."`` identifier when
    ``escapes_in_names`` (Snowflake: strings only). A line comment ends at
    the first CR or LF. Each ``"..."`` identifier is appended to ``quoted``.
    :class:`_Unreadable` when a quote or a block comment never closes, a
    line comment ends at a bare CR, or a block comment holds a ``/*``."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in _QUOTES:
            escapes = escapes_in_strings if ch == "'" else escapes_in_names
            j = i + 1
            while True:
                if j >= n:
                    raise _Unreadable(
                        "it has an unterminated string or quoted identifier"
                    )
                if escapes and sql[j] == "\\":
                    j += 2
                    continue
                if sql[j] == ch:
                    if j + 1 < n and sql[j + 1] == ch:
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            if ch == '"' and quoted is not None:
                quoted.append(_Quoted(sql[i + 1 : j - 1].replace('""', '"'), j))
            out.append(" " * (j - i))
            i = j
        elif sql.startswith("--", i) or sql.startswith("//", i):
            # To the line's end: the first CR or LF (a reader that stops only
            # at LF hides what follows a bare CR, and sqlglot stops at both).
            line_end = _LINE_END.search(sql, i)
            end = n if line_end is None else line_end.start()
            if sql.startswith("\r", end) and not sql.startswith("\r\n", end):
                raise _Unreadable(
                    "it has a -- or // comment ended by a bare CR "
                    "(readers disagree where it ends)"
                )
            out.append(" " * (end - i))
            i = end
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end < 0:
                raise _Unreadable("it has an unterminated /* */ comment")
            # Up to the closer's '*' included: in '/* a /*/', a nesting reader
            # takes '/*' first and the comment runs on.
            if "/*" in sql[i + 2 : end + 1]:
                raise _Unreadable(
                    "it has a /* inside a /* */ comment (readers disagree whether "
                    "comments nest)"
                )
            out.append(" " * (end + 2 - i))
            i = end + 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


# -- the parser ----------------------------------------------------------------------


def _tokens_and_statements(sql: str) -> tuple[list[Token], list[exp.Expression]]:
    """sqlglot's tokens of ``sql`` and every statement in it (Snowflake
    dialect, silently: nothing sqlglot logs while it parses gets out)."""
    with _sqlglot_silenced():
        tokens = _DIALECT.tokenize(sql)
        parser = _QuietParser(dialect=_DIALECT)
        return tokens, [s for s in parser.parse(tokens, sql) if s is not None]


def _parse(sql: str) -> list[exp.Expression]:
    return _tokens_and_statements(sql)[1]


def _parsed_problem(sql: str) -> str | None:
    try:
        tokens, statements = _tokens_and_statements(sql)
    except SqlglotError:
        return "it does not parse as Snowflake SQL"
    except Exception:  # noqa: BLE001 — a parser bug refuses, never admits
        return "it does not parse as Snowflake SQL"
    if len(statements) != 1:
        return "it is more than one statement" if statements else "it is empty"
    (root,) = statements
    if not isinstance(root, _QUERY_ROOTS):
        return f"it is a {root.key.upper()} statement, not a query"
    problem = _token_problem(tokens)
    if problem is not None:
        return problem
    for node in root.walk():
        problem = _node_problem(node)
        if problem is not None:
            return problem
    return None


def _token_problem(tokens: list[Token]) -> str | None:
    """sqlglot's reading, token by token: no ``$`` or ``@`` token, no
    ``$$`` string, no name (a word or a quoted identifier) holding ``$``,
    no ``!`` method call, no ``AI_*`` or ``IDENTIFIER`` call, no ``CORTEX``
    qualifier. A string literal's text is data, and is not looked at."""
    for index, token in enumerate(tokens):
        kind = token.token_type
        if kind in _DOLLAR_QUOTED_TOKENS:
            return _DOLLAR_STRING
        if kind == TokenType.PARAMETER:
            return _PARAMETER
        if kind in _STRING_TOKENS:
            continue
        if "$" in token.text:
            return _DOLLAR_NAME
        if token.text == "!" and kind != TokenType.IDENTIFIER:  # not a "!" name
            return _MODEL_METHOD
        following = tokens[index + 1].token_type if index + 1 < len(tokens) else None
        if following == TokenType.L_PAREN:
            problem = _called_problem(token.text)
            if problem is not None:
                return problem
        elif following == TokenType.DOT:
            problem = _qualifier_problem(token.text)
            if problem is not None:
                return problem
    return None


def _node_problem(node: exp.Expression) -> str | None:
    """sqlglot's tree, node by node."""
    if isinstance(node, _WRITE_NODES):
        return f"it contains {node.key.upper()}"
    if isinstance(node, _DOLLAR_QUOTED_NODES):
        return _DOLLAR_STRING
    if isinstance(node, _MODEL_METHOD_NODES):
        return _MODEL_METHOD
    if isinstance(node, _NAMED_NODES):
        name = str(node.name)
        if name.upper() in _NEVER_NAMED:
            return f"it names {name.upper()}"
        if "$" in name:
            return _DOLLAR_NAME
    if isinstance(node, exp.Func):
        for name in _function_names(node):
            problem = _called_problem(name)
            if problem is not None:
                return problem
    if (
        isinstance(node, exp.Identifier)
        and isinstance(node.parent, exp.Dot)
        and _qualifier_problem(str(node.name)) is not None
    ):
        return _CORTEX
    if isinstance(node, _TABLE_FUNCTION_NODES) and _calls_unlisted(node.this):
        return _table_function_problem()
    if isinstance(node, exp.Table):
        if str(node.name).startswith("@"):
            return _STAGE
        if isinstance(node.this, exp.Func) and (
            _calls_unlisted(node.this)
            # A qualified table function in a bare FROM or JOIN
            # (``FROM s.FLATTEN(...)``): a user-defined function.
            or node.args.get("db") is not None
            or node.args.get("catalog") is not None
        ):
            return _table_function_problem()
    return None


def _function_names(node: exp.Func) -> list[str]:
    """The names a function node can stand for: as written for an unknown
    one, else every SQL name of its type (``AIClassify``: ``AI_CLASSIFY``)."""
    if isinstance(node, exp.Anonymous):
        return [str(node.name)]
    try:
        return [str(name) for name in node.sql_names()] or [node.sql_name()]
    except Exception:  # noqa: BLE001 — a type without names: its key
        return [node.key]


def _calls_unlisted(called: object) -> bool:
    """Whether a ``TABLE(...)`` / ``LATERAL ...`` calls anything but a
    function in :data:`TABLE_FUNCTIONS` (FLATTEN parses as ``Explode``). A
    qualified call (``s.FLATTEN(...)``) parses as a ``Dot``: refused."""
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
    "check_context",
    "check_select_only",
    "table_references",
]
