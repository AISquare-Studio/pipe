"""SELECT-only SQL for Databricks: two independent checks, both before any HTTP call.

Either one refusing refuses the statement (:class:`NotReadOnly`, whose
message says why and never echoes the SQL; a refused function is named by
its canonical name only). Each one refuses every write on its own, so
neither is a backup for the other: the suite runs each layer ALONE against a
reference reading of Spark's lexer (``tests/test_sqlguard.py``), and
whatever that layer admits holds no write in Spark's reading. What only one
of them refuses is pinned there too (none of it is a write in Spark's
reading), so dropping either layer fails the suite.

The two read the text differently. The lexer decides where Spark's tokens
end and refuses wherever ANY two readers could disagree; the parser reads
the text as sqlglot does, refuses wherever sqlglot's reading and Spark's
differ, and adds the statement's structure.

1. **The lexer** (no parser) reads the text as Spark's own lexer does
   (``SqlBaseLexer.g4``), and refuses wherever two readers could disagree on
   where a token ends, rather than pick one:

   - a ``--`` comment ends at the first CR or LF. Spark carries it on past a
     backslash right before the LF (``SIMPLE_COMMENT: '--' ('\\\\\\n' |
     ~[\\r\\n])*``) and sqlglot does not, so no comment may end in a
     backslash (either kind, at a line break or at the end of the text); a
     ``--`` comment a bare CR ends (a CR not followed by LF) is refused too,
     as some readers don't take it as a line end;
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
   - ``$`` outside quotes and comments is refused: Databricks reads
     ``$$...$$`` as text in places (routine bodies) and sqlglot reads ``$``
     as a parameter; no read-only query needs one there (parameter markers
     are ``:name`` and ``?``);
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
   ``reflect``, ``java_method``, ``secret``, ``list_secrets``,
   ``vector_search``, ``IDENTIFIER(...)``, and any ``ai_*``), plainly or as
   a quoted name, and has no ``USING '<script>'`` (``SELECT TRANSFORM(...)
   USING 'cmd'`` runs a command).

2. **The parser** (sqlglot, Databricks dialect). First, sqlglot's own tokens
   must read the way Spark's lexer does. Each of these is a place the two
   lexers differ, and is refused:

   - ``${`` anywhere (Spark substitutes it before any reader sees the text);
   - a comment that ends in a backslash: Spark carries a ``--`` comment on
     past a backslash right before its line break, and sqlglot ends it there
     (the shape of seq 13947 FAIL 1);
   - a ``/*+`` outside a quoted token that is not a hint sqlglot parsed
     (right after ``SELECT``) whose insides are only names, numbers, commas
     and parentheses. Spark reads a hint's insides as tokens wherever it
     stands; sqlglot reads ``/*+`` anywhere else as a comment, and nests it
     inside a ``/* */`` comment, where Spark doesn't;
   - ``{#`` outside a quoted token: sqlglot reads ``{# ... #}`` as a (Jinja)
     comment in every dialect, Spark doesn't;
   - ``*`` right before ``/``: Spark reads ``*/`` as one token (a hint's
     end), so in ``*/*`` it opens no comment; sqlglot reads ``*`` and then a
     comment;
   - a ``"..."`` string that ends elsewhere when a backslash escapes nothing:
     under ``spark.sql.ansi.doubleQuotedIdentifiers`` Spark reads ``"..."``
     as an identifier, with no backslash escapes;
   - a non-ASCII character in a token that is not quoted (Spark 3 upper-cases
     with ``Character.toUpperCase``).

   Then the text parses into exactly ONE statement whose root is a query
   (``SELECT``, ``WITH ... SELECT``, a ``UNION``/``INTERSECT``/``EXCEPT`` of
   queries, or a parenthesised one), and no node anywhere in its tree is a
   write, DDL, session, transaction or script-transform node (``INSERT`` ...
   ``SELECT ... INTO``, ``SET``, ``USE``, ``COPY``, ``TRANSFORM ... USING``,
   an unparsed ``Command``, ...) or a call of a side-effect function. Text it
   can't tokenize or parse is refused.

   sqlglot logs pieces of the SQL while it parses ("Invalid JSON path
   syntax. ... <the path>" at WARNING for a JSON key with a dash, in
   statements this guard admits). So it tokenizes and parses inside
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
from sqlglot.dialects.dialect import Dialect
from sqlglot.errors import SqlglotError
from sqlglot.tokens import Token, TokenType

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
#: method by reflection, a secret's value or the names of every secret
#: scope and key, a vector search (it bills an embedding endpoint for its
#: ``query_text``), and a name built from a string (``IDENTIFIER('...')``,
#: which the checks can't read through). Any ``ai_*`` call (model serving,
#: billed outside the warehouse) is refused too.
SIDE_EFFECT_FUNCTIONS = frozenset(
    {
        "HTTP_REQUEST",
        "REMOTE_QUERY",
        "REFLECT",
        "JAVA_METHOD",
        "TRY_REFLECT",
        "SECRET",
        "TRY_SECRET",
        "LIST_SECRETS",
        "VECTOR_SEARCH",
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
_SUBSTITUTION = "${"


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


def _calls_label(name: str) -> str:
    """Why a call of ``name`` (a side-effect function) is refused, naming it
    by its canonical name only: the caller's spelling (``ai_<anything>``, a
    quoted name) is their text, which a refusal never echoes."""
    upper = name.upper()
    if upper in SIDE_EFFECT_FUNCTIONS:
        return f"it calls {upper}"
    return "it calls an AI_* function"


# -- the parser ----------------------------------------------------------------------

#: sqlglot token types whose text was quoted: strings and backtick identifiers.
_QUOTED_TOKENS = frozenset(
    getattr(TokenType, name)
    for name in (
        "STRING",
        "RAW_STRING",
        "NATIONAL_STRING",
        "HEX_STRING",
        "BIT_STRING",
        "BYTE_STRING",
        "UNICODE_STRING",
        "HEREDOC_STRING",
        "IDENTIFIER",
    )
    if hasattr(TokenType, name)
)
#: sqlglot's string tokens that take backslash escapes (not the raw ones).
_ESCAPED_STRINGS = frozenset(
    getattr(TokenType, name)
    for name in ("STRING", "NATIONAL_STRING")
    if hasattr(TokenType, name)
)
#: What a hint may hold for Spark and sqlglot to read it alike: names,
#: numbers, commas and parentheses (``BROADCAST(t)``, ``REPARTITION(8, c)``).
_PLAIN_HINT = re.compile(r"[A-Za-z0-9_(),.\s]*")
_HINT_OPEN = "/*+"
_HINT_CLOSE = "*/"
_JINJA_COMMENT = "{#"


def _tokens(sql: str) -> list[Token]:
    """sqlglot's tokens of ``sql`` (Databricks dialect), silently."""
    with quiet_sqlglot():
        return list(Dialect.get_or_raise("databricks").tokenize(sql))


def _parse(sql: str, tokens: list[Token]) -> list[exp.Expression]:
    """Every statement in ``tokens`` (sqlglot, Databricks dialect, silently:
    nothing sqlglot logs while it parses gets out)."""
    with quiet_sqlglot():
        parser = Dialect.get_or_raise("databricks").parser()
        return [s for s in parser.parse(tokens, sql) if s is not None]


def _function_name(node: exp.Expression) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name)
    if isinstance(node, exp.Func):
        return node.sql_name()
    return ""


def _parsed(sql: str) -> str | None:
    if _SUBSTITUTION in sql:
        return "it has ${...}, which Spark substitutes before it reads the SQL"
    try:
        tokens = _tokens(sql)
        problem = _read_unlike_spark(sql, tokens)
        if problem is not None:
            return problem
        statements = _parse(sql, tokens)
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
            return _calls_label(name.rsplit(".", 1)[-1])
    return None


def _read_unlike_spark(sql: str, tokens: list[Token]) -> str | None:
    """Where sqlglot's tokens of ``sql`` and Spark's lexer read the text
    differently (the module docstring lists each place), or None. Read from
    sqlglot's tokens, comments and token positions alone."""
    quoted: list[tuple[int, int]] = []
    hints: dict[int, Token] = {}
    for token in tokens:
        if any(comment.endswith("\\") for comment in token.comments or ()):
            return (
                "a comment ends in a backslash (Spark carries a -- comment on past "
                "a backslash at its line break, sqlglot doesn't)"
            )
        kind = token.token_type
        if kind in _QUOTED_TOKENS:
            quoted.append((token.start, token.end))
            if kind in _ESCAPED_STRINGS and not _ends_alike_unescaped(sql, token):
                return (
                    'a backslash in a "..." string makes where it ends depend on the '
                    "reader (Spark can read it as an identifier, with no escapes)"
                )
        elif kind == TokenType.HINT:
            hints[token.start] = token
        elif not token.text.isascii():
            return (
                "it has a non-ASCII character outside quotes and comments (write such "
                "a name in backticks)"
            )
        if kind == TokenType.STAR and sql.startswith("/", token.end + 1):
            # A '*' token (not inside a comment) right before '/': Spark's
            # '*/' token, which sqlglot reads as '*' and then '/' or '/*'.
            return "it has */ outside a comment (Spark reads it as one token)"

    def unquoted(position: int) -> bool:
        return not any(start <= position <= end for start, end in quoted)

    for position in _find_all(sql, _JINJA_COMMENT):
        if unquoted(position):
            return "it has {# (sqlglot reads {# #} as a comment, Spark doesn't)"
    for position in _find_all(sql, _HINT_OPEN):
        if not unquoted(position):
            continue
        hint = hints.get(position)
        if hint is None:
            return (
                "it has a /*+ hint that is not right after SELECT, or one inside a "
                "comment (Spark reads its insides as SQL, sqlglot as a comment)"
            )
        inside = hint.text[len(_HINT_OPEN) : -len(_HINT_CLOSE)]
        if not hint.text.endswith(_HINT_CLOSE) or not _PLAIN_HINT.fullmatch(inside):
            return (
                "a /*+ hint holds more than names, numbers, commas and parentheses "
                "(Spark reads its insides as SQL, sqlglot as a comment)"
            )
    return None


def _ends_alike_unescaped(sql: str, token: Token) -> bool:
    """Whether a ``"..."`` string token ends where it would if a backslash
    escaped nothing (a ``'...'`` one always takes escapes in Spark)."""
    opening = (
        token.start
        if sql[token.start : token.start + 1] in _QUOTES
        else token.start + 1
    )
    if sql[opening : opening + 1] != '"' or "\\" not in sql[opening : token.end + 1]:
        return True
    return _quoted_end(sql, opening, backslash_escapes=False) == _quoted_end(
        sql, opening, backslash_escapes=True
    )


def _find_all(text: str, needle: str) -> list[int]:
    found: list[int] = []
    at = text.find(needle)
    while at >= 0:
        found.append(at)
        at = text.find(needle, at + 1)
    return found


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
            return _calls_label(word.group())
        if word.group().upper() == "USING" and _script_follows(text, word.end()):
            return "it runs a script (TRANSFORM ... USING '...')"
    for name in quoted:
        # A `name` or "name" (an identifier under ANSI double quotes) called.
        if (
            text[name.start] != "'"
            and _side_effect(name.name.rsplit(".", 1)[-1])
            and _calls(text, name.end)
        ):
            return _calls_label(name.name.rsplit(".", 1)[-1])
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


_ENDS_IN_A_BACKSLASH = _Unreadable(
    "a comment ends in a backslash (Spark carries a -- comment on past a "
    "backslash at its line break, other readers don't)"
)


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
    if _SUBSTITUTION in sql:
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
            if sql[end - 1] == "\\" and end - 1 >= i + 2:
                # Before a line break Spark reads on; at the end of the text
                # it doesn't, but sqlglot's reading can't tell the two apart.
                raise _ENDS_IN_A_BACKSLASH
            if (
                line_end is not None
                and sql[end] == "\r"
                and not sql.startswith("\r\n", end)
            ):
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
            if sql[end - 1] == "\\" and end - 1 >= i + 2:
                raise _ENDS_IN_A_BACKSLASH
            blank(i, end + 2)
            i = end + 2
        elif sql.startswith("*/", i):
            # One token in Spark (a hint's end), so its '/' opens no comment.
            i += 2
        elif ch == "\\":
            raise _Unreadable("it has a backslash outside quotes")
        elif ch == "$":
            raise _Unreadable(
                "it has a $ outside quotes and comments (Databricks reads $$...$$ as "
                "text in places, other readers as a parameter)"
            )
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
