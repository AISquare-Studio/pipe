"""The SELECT-only guard: each check refuses on its own, and the two together.

The lexer and the parser are tested apart (``_lex`` / ``_parsed_problem``), so
removing either one fails a test here, and together (``check_select_only``)
against the bypasses found in the BigQuery guard (seq 13647) translated to
Redshift's readers.
"""

from __future__ import annotations

import io
import logging
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from aisquare.pipe.core import sql as core_sql

from aisquare_pipe_aws.redshift import sqlguard
from aisquare_pipe_aws.redshift.sqlguard import (
    MAX_SQL_CHARS,
    NotReadOnly,
    check_select_only,
    quote_identifier,
)

ADMITTED = [
    "SELECT 1",
    "select id, title from policies.rules where id = 3",
    "SELECT 1;",
    "SELECT 1; -- a comment after the one trailing semicolon",
    (
        "WITH recent AS (SELECT * FROM policies.rules WHERE updated_at > '2026-01-01') "
        "SELECT id FROM recent"
    ),
    "SELECT 'it''s', \"odd\"\"name\" FROM t /* a comment */",
    "SELECT 'a\\\\b' FROM t",  # an escaped backslash ends the same either way
    'SELECT "comment", "update", "into" FROM t',
    "SELECT * FROM t1 UNION ALL SELECT * FROM t2",
    "SELECT a FROM t1 INTERSECT SELECT a FROM t2",
    "SELECT a FROM t1 EXCEPT SELECT a FROM t2",
    "(SELECT 1)",
    "SELECT CASE WHEN a > 1 THEN 'x' ELSE 'y' END AS bucket FROM t",
    "SELECT created_at, updated_by, settings FROM t",  # words containing keywords
    "SELECT 'DROP TABLE t; DELETE FROM t' AS text",  # keywords in a string
    "SELECT 1 -- DROP TABLE t\n",
    "SELECT 1 /* ; DROP TABLE t */",
    "SELECT *\r\nFROM t\rWHERE a = 1",
    "SELECT * FROM #temp_rules",
    "SELECT TOP 10 * FROM t ORDER BY id",
    "SELECT listagg(title, ',') WITHIN GROUP (ORDER BY id) FROM t",
    # Numbers, exponents included, beside words that are no keyword.
    "SELECT 1e5 AS big, 1.5E-3 AS small, .5e+2 AS half, 2.e1 FROM t",
    "SELECT col1e5into, x2e3 FROM t",  # one identifier each, digits and all
    "SELECT t.a1, 3.14 FROM t",
]


@pytest.mark.parametrize("sql", ADMITTED)
def test_one_read_only_query_is_admitted(sql):
    statement = check_select_only(sql)
    assert statement == statement.strip()
    assert not statement.rstrip().endswith(";")


def test_the_trailing_semicolon_and_whitespace_are_dropped():
    assert check_select_only("  SELECT 1 ;  -- done\n") == "SELECT 1"


#: The BigQuery bypass (seq 13647) verbatim: a bare CR ends the '--' comment,
#: and a nesting reader hides the DROP inside a /* /* */ comment.
BIGQUERY_CHAIN = "SELECT 1 --\r/* /* */; DROP TABLE ds.t; SELECT 1 /* */ -- */"

REFUSED = {
    "bigquery-chain": BIGQUERY_CHAIN,
    "bare-cr-ends-a-comment": "SELECT 1 --\r; DROP TABLE t",
    "nested-comment": "SELECT 1 /* /* */ ; DROP TABLE t; SELECT 1 /* */ */",
    "nested-comment-sharing-a-star": "SELECT 1 /* a /*/ ; DROP TABLE t; /* */",
    "backslash-ends-a-string-early": "SELECT 'a\\'; DROP TABLE t; --'",
    "backslash-in-an-e-string": "SELECT E'\\'' ; DROP TABLE t; --'",
    "backslash-in-an-identifier": 'SELECT "a\\"; DROP TABLE t; --"',
    "dollar-quoting": "SELECT $$a; DROP TABLE t; $$",
    "dollar-tag-quoting": "SELECT $q$a; DROP TABLE t; $q$",
    "dollar-parameter": "SELECT * FROM t WHERE id = $1",
    "backslash-outside-quotes": "SELECT 1 \\; DROP TABLE t",
    "nul": "SELECT 1\x00; DROP TABLE t",
    "two-statements": "SELECT 1; SELECT 2",
    "select-then-drop": "SELECT 1; DROP TABLE t",
    "hash-is-not-a-comment": "SELECT 1 # ; DROP TABLE t",
    "select-into": "SELECT * INTO new_table FROM t",
    "select-into-temp": "SELECT * INTO TEMP scratch FROM t",
    "insert-cte": "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x",
    "delete-cte": "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x",
    "insert": "INSERT INTO t VALUES (1)",
    "update": "UPDATE t SET a = 1",
    "delete": "DELETE FROM t",
    "merge": "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN DELETE",
    "create-table-as": "CREATE TABLE t2 AS SELECT * FROM t",
    "drop": "DROP TABLE t",
    "truncate": "TRUNCATE t",
    "alter": "ALTER TABLE t ADD COLUMN c int",
    "grant": "GRANT SELECT ON t TO PUBLIC",
    "copy": "COPY t FROM 's3://bucket/key' IAM_ROLE default",
    "unload": "UNLOAD ('SELECT * FROM t') TO 's3://bucket/out' IAM_ROLE default",
    "call": "CALL my_procedure()",
    "set": "SET search_path TO private",
    "begin": "BEGIN; SELECT 1",
    "vacuum": "VACUUM t",
    "explain": "EXPLAIN SELECT 1",
    "show": "SHOW TABLES",
    "terminate-a-session": "SELECT pg_terminate_backend(12345)",
    "cancel-a-session": "SELECT pg_cancel_backend(12345)",
    "set-config": "SELECT set_config('search_path', 'private', false)",
    # The same calls through double-quoted names (seq 13800 3b).
    "quoted-terminate": 'SELECT "pg_terminate_backend"(12345)',
    "quoted-upper-terminate": 'SELECT "PG_TERMINATE_BACKEND"(12345)',
    "qualified-quoted-terminate": 'SELECT pg_catalog."pg_terminate_backend"(12345)',
    "quoted-cancel": 'SELECT "pg_cancel_backend"(12345)',
    "quoted-set-config": """SELECT "set_config"('search_path', 'private', false)""",
    "unquoted-keyword-column": "SELECT update FROM t",
    "for-update": "SELECT * FROM t FOR UPDATE",
    # A keyword glued to a number's exponent (seq 13894 m1): Redshift's lexer
    # (PostgreSQL 8.0's) reads 1e5INTO as the number 1e5, then INTO.
    "exponent-glued-into": "SELECT 1e5INTO t FROM x",
    "exponent-glued-into-temp": "SELECT 1e5INTO TEMP t FROM x",
    "decimal-exponent-glued-into-table": "SELECT 1.5e3INTO TABLE t",
    "dot-exponent-glued-into-unlogged": "SELECT .5E1INTO UNLOGGED t",
    "signed-exponent-glued-into": "SELECT 1e-5INTO t FROM x",
    # A Unicode-escaped name (seq 13894 n1): PostgreSQL 8.4+ decodes
    # U&"pg_terminate_backen\0064" to pg_terminate_backend.
    "unicode-escaped-terminate": 'SELECT U&"pg_terminate_backen\\0064"(1)',
    "unicode-escaped-lowercase-u": 'SELECT u&"pg_terminate_backen\\0064"(1)',
    "unicode-escaped-string": "SELECT U&'\\0041' AS a",
    "unterminated-string": "SELECT 'abc",
    "unterminated-identifier": 'SELECT "abc',
    "unterminated-comment": "SELECT 1 /* never closed",
    "empty": "   ",
    "only-a-comment": "-- nothing\n",
    "only-a-semicolon": ";",
}


@pytest.mark.parametrize("sql", list(REFUSED.values()), ids=list(REFUSED))
def test_anything_but_one_read_only_query_is_refused(sql):
    with pytest.raises(NotReadOnly):
        check_select_only(sql)


#: Refused by the lexer alone: where Redshift and a reader could disagree.
LEXER_REFUSES = [
    "bigquery-chain",
    "bare-cr-ends-a-comment",
    "nested-comment",
    "nested-comment-sharing-a-star",
    "backslash-ends-a-string-early",
    "backslash-in-an-e-string",
    "backslash-in-an-identifier",
    "dollar-quoting",
    "dollar-tag-quoting",
    "dollar-parameter",
    "backslash-outside-quotes",
    "nul",
    "two-statements",
    "select-then-drop",
    "hash-is-not-a-comment",
    "select-into",
    "insert-cte",
    "delete-cte",
    "unload",
    "copy",
    "terminate-a-session",
    "cancel-a-session",
    "set-config",
    "quoted-terminate",
    "quoted-upper-terminate",
    "qualified-quoted-terminate",
    "quoted-cancel",
    "quoted-set-config",
    "for-update",
    "unquoted-keyword-column",
    "exponent-glued-into",
    "exponent-glued-into-temp",
    "decimal-exponent-glued-into-table",
    "dot-exponent-glued-into-unlogged",
    "signed-exponent-glued-into",
    "unicode-escaped-terminate",
    "unicode-escaped-lowercase-u",
    "unicode-escaped-string",
]


@pytest.mark.parametrize("name", LEXER_REFUSES)
def test_the_lexer_alone_refuses(name):
    assert sqlguard._lex(REFUSED[name]).problem is not None


#: Refused by the parser alone (sqlglot, Redshift dialect).
PARSER_REFUSES = [
    "bare-cr-ends-a-comment",
    "two-statements",
    "select-then-drop",
    "hash-is-not-a-comment",
    "select-into",
    "select-into-temp",
    "insert-cte",
    "insert",
    "update",
    "delete",
    "create-table-as",
    "drop",
    "alter",
    "grant",
    "copy",
    "unload",
    "set",
    "begin",
    "explain",
    "terminate-a-session",
    "cancel-a-session",
    "set-config",
    "quoted-terminate",
    "quoted-upper-terminate",
    "qualified-quoted-terminate",
    "quoted-cancel",
    "quoted-set-config",
    "for-update",
]


@pytest.mark.parametrize("name", PARSER_REFUSES)
def test_the_parser_alone_refuses(name):
    assert sqlguard._parsed_problem(REFUSED[name]) is not None


#: Text the lexer admits (one statement, SELECT first, no listed word) that
#: only the parser refuses: so the check must consult it.
PARSER_ONLY = [
    "SELECT * FROM t FOR SHARE",  # a lock (exp.Lock), though SHARE is no write word
    "SELECT (1",
    "SELECT 1 FROM",
    "WITH x AS (SELECT 1)",
    "SELECT * FROM t1 JOIN",
]


@pytest.mark.parametrize("sql", PARSER_ONLY)
def test_the_check_refuses_what_only_the_parser_refuses(sql):
    assert sqlguard._lex(sql).problem is None
    with pytest.raises(NotReadOnly):
        check_select_only(sql)


def test_the_parser_alone_admits_what_redshift_may_read_as_two_statements():
    """Why the lexer refuses ambiguity: sqlglot nests comments and reads a
    backslash as an escape, so on its own it would admit these, and a
    Redshift reading the other way would run the DROP."""
    hidden = [
        REFUSED["nested-comment"],
        REFUSED["backslash-ends-a-string-early"],
        REFUSED["dollar-quoting"],
    ]
    admitted = [sql for sql in hidden if sqlguard._parsed_problem(sql) is None]
    assert admitted, "sqlglot now refuses these itself; the lexer still must"
    for sql in admitted:
        assert sqlguard._lex(sql).problem is not None


def test_the_bigquery_chain_is_refused_by_both_readings_of_its_comments():
    problem = sqlguard._lex(BIGQUERY_CHAIN).problem
    assert problem is not None and "/*" in problem


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 --\r; DROP TABLE t",
        "SELECT 1 --\rDROP TABLE t",
        "SELECT a FROM t --\r\n; DELETE FROM t",
    ],
)
def test_a_dash_comment_ends_at_a_bare_cr(sql):
    blanked, _ = sqlguard._blank(sql)
    assert "DROP" in blanked or "DELETE" in blanked


def test_a_long_statement_is_refused():
    with pytest.raises(NotReadOnly, match="longer than"):
        check_select_only("SELECT 1 " + " " * MAX_SQL_CHARS)


@pytest.mark.parametrize("sql", list(REFUSED.values()), ids=list(REFUSED))
def test_a_refusal_never_echoes_the_sql(sql):
    with pytest.raises(NotReadOnly) as raised:
        check_select_only(sql)
    message = str(raised.value)
    for fragment in ("DROP TABLE", "new_table", "s3://", "12345", "private"):
        assert fragment not in message


# sqlglot logs a JSON path it can't read ("Invalid JSON path syntax. ...
# <the path>") at WARNING on the "sqlglot" logger: a key with a dash, a space
# or an open bracket is one. Redshift's parser reaches it through the generic
# JSON_EXTRACT / JSON_EXTRACT_SCALAR builders and PostgreSQL's JSONB_EXISTS.
# These are valid queries, and the guard ADMITS them (seq 13800).
ADMITTED_WITH_JSON_PATHS = [
    "SELECT JSON_EXTRACT(payload, 'member.ssn-123-45-6789') FROM claims",
    "SELECT JSON_EXTRACT(p, 'patient-4111111111111111') FROM claims",
    (
        "WITH c AS (SELECT JSON_EXTRACT_SCALAR(p, 'dob-1970-01-01') AS dob "
        "FROM claims) SELECT dob FROM c"
    ),
    "SELECT JSONB_EXISTS(p, 'member.ssn-123-45-6789') FROM claims",
    "SELECT JSON_EXTRACT(p, 'ssn 123 45 6789') FROM claims",
    "SELECT JSON_EXTRACT(p, 'a[') FROM claims",
    "SELECT JSON_EXTRACT(p, '$.member.ssn-123') FROM claims",
    "SELECT id FROM claims WHERE JSON_EXTRACT(p, 'diagnosis-F32.1') IS NOT NULL",
]
# The lexer passes these, and the parser logs the path before it refuses them.
REFUSED_WITH_JSON_PATHS = [
    "SELECT JSON_EXTRACT(p, 'member.ssn-123-45-6789') FROM claims FOR SHARE",
    "SELECT JSON_EXTRACT(p, 'member.ssn-123-45-6789') FROM",
]
# Pieces of those paths: none may reach any log.
PATH_SECRETS = (
    "ssn",
    "123-45-6789",
    "4111111111111111",
    "1970-01-01",
    "F32.1",
    "patient",
    "member",
    "a[",
)


@pytest.fixture
def sqlglot_records(caplog):
    """Every record the "sqlglot" logger emits, at DEBUG and up: through
    caplog's root handler and through a handler on the logger itself."""
    records: list[logging.LogRecord] = []

    class Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    sqlglot_logger = logging.getLogger("sqlglot")
    keep = Keep(level=logging.DEBUG)
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="sqlglot")
    sqlglot_logger.addHandler(keep)
    try:
        yield records
    finally:
        sqlglot_logger.removeHandler(keep)


def _run_the_guard_over_everything() -> None:
    for sql in [*ADMITTED, *REFUSED.values(), *REFUSED_WITH_JSON_PATHS]:
        try:
            check_select_only(sql)
        except NotReadOnly:
            pass
    for sql in ADMITTED_WITH_JSON_PATHS:
        assert check_select_only(sql) == sql
    for sql in REFUSED_WITH_JSON_PATHS:
        with pytest.raises(NotReadOnly):
            check_select_only(sql)


def test_nothing_is_logged(caplog, sqlglot_records):
    _run_the_guard_over_everything()
    assert [r.getMessage() for r in sqlglot_records] == []
    assert not [r for r in caplog.records if r.name.startswith("sqlglot")]
    assert "DROP" not in caplog.text
    for secret in PATH_SECRETS:
        assert secret not in caplog.text


@contextmanager
def _only_last_resort() -> Iterator[io.StringIO]:
    """Python's own fallback, as on a host that never configures logging: no
    handler on any logger, so a WARNING goes to ``logging.lastResort``
    (stderr; here a buffer). Entered inside the test body: pytest adds its
    capture handlers to the root logger after fixtures run."""
    stream = io.StringIO()
    fallback = logging.StreamHandler(stream)
    fallback.setLevel(logging.WARNING)
    loggers = [logging.getLogger(), logging.getLogger("sqlglot")]
    saved = [list(logger.handlers) for logger in loggers]
    real_last_resort = logging.lastResort
    logging.lastResort = fallback
    for logger in loggers:
        logger.handlers.clear()
    try:
        yield stream
    finally:
        logging.lastResort = real_last_resort
        for logger, handlers in zip(loggers, saved, strict=True):
            logger.handlers[:] = handlers


def test_the_last_resort_handler_prints_nothing():
    with _only_last_resort() as stderr:
        _run_the_guard_over_everything()
        guarded = stderr.getvalue()
        # The fallback is live: a record from outside the guard reaches it.
        logging.getLogger("sqlglot").warning("outside the guard")
    assert guarded == ""
    assert stderr.getvalue() == "outside the guard\n"


def test_a_python_with_no_logging_set_up_prints_nothing():
    """The repro of seq 13800 verbatim: a fresh ``python -I`` (no logging
    configured, so lastResort prints to stderr) runs the guard over every
    JSON-path statement. Its sockets are blocked too."""
    src = Path(__file__).resolve().parents[1] / "src"
    statements = [*ADMITTED_WITH_JSON_PATHS, *REFUSED_WITH_JSON_PATHS]
    code = "\n".join(
        [
            "import socket, sys",
            "def refuse(*a, **k): raise OSError('network access attempted')",
            "socket.socket.connect = refuse",
            "socket.create_connection = refuse",
            f"sys.path.insert(0, {str(src)!r})",
            "from aisquare_pipe_aws.redshift.sqlguard import NotReadOnly, check_select_only",
            f"for sql in {statements!r}:",
            "    try:",
            "        print('admitted' if check_select_only(sql) == sql else 'changed')",
            "    except NotReadOnly:",
            "        print('refused')",
        ]
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    assert done.stdout.split() == ["admitted"] * len(ADMITTED_WITH_JSON_PATHS) + [
        "refused"
    ] * len(REFUSED_WITH_JSON_PATHS)


def test_sqlglot_still_logs_outside_the_guard(sqlglot_records):
    """The guard quiets sqlglot only while it parses: no global switch."""
    import sqlglot

    check_select_only(ADMITTED_WITH_JSON_PATHS[0])
    sqlglot.parse_one(
        "SELECT JSON_EXTRACT(p, 'outside-the-guard') FROM t", read="redshift"
    )
    logging.getLogger("sqlglot").warning("after the guard")
    messages = [r.getMessage() for r in sqlglot_records]
    assert any("outside-the-guard" in m for m in messages), messages
    assert "after the guard" in messages
    assert logging.root.manager.disable == logging.NOTSET
    assert not logging.getLogger("sqlglot").disabled


@pytest.mark.parametrize("hosts_own_filter", [False, True])
def test_the_guard_stays_quiet_if_its_filter_was_removed(
    sqlglot_records, monkeypatch, hosts_own_filter
):
    """A host that resets the "sqlglot" logger's filters (and maybe sets its
    own) doesn't bring the leak back: the guard re-attaches its own on the
    next parse."""
    sqlglot_logger = logging.getLogger("sqlglot")
    monkeypatch.setattr(sqlglot_logger, "filters", [])
    if hosts_own_filter:
        sqlglot_logger.addFilter(lambda record: True)
    assert check_select_only(ADMITTED_WITH_JSON_PATHS[0])
    assert sqlglot_records == []
    # The shared helper's filter (aisquare.pipe.core.sql), back on the logger.
    assert core_sql._FILTER in sqlglot_logger.filters


def test_an_old_sqlglot_refuses_every_statement(monkeypatch):
    """The [aws] extra needs sqlglot >= 26.25; a host that got an older one
    from another package fails closed."""
    import sqlglot

    monkeypatch.setattr(sqlglot, "__version__", "26.24.0")
    with pytest.raises(NotReadOnly, match="needs sqlglot 26.25 or later"):
        check_select_only("SELECT 1")
    monkeypatch.setattr(sqlglot, "__version__", "26.25.0")
    assert check_select_only("SELECT 1") == "SELECT 1"


def test_another_thread_logging_through_sqlglot_meanwhile_is_untouched(
    sqlglot_records, monkeypatch
):
    """The filter drops records only in the context that is parsing: a
    thread logging through sqlglot while the guard parses still logs."""
    import threading

    real_parse = sqlguard._QuietParser.parse
    logged = threading.Event()

    def parse_while_another_thread_logs(self, *args, **kwargs):
        def other() -> None:
            logging.getLogger("sqlglot").warning("another thread")
            logged.set()

        thread = threading.Thread(target=other)
        thread.start()
        thread.join(timeout=10)
        return real_parse(self, *args, **kwargs)

    monkeypatch.setattr(sqlguard._QuietParser, "parse", parse_while_another_thread_logs)
    check_select_only(ADMITTED_WITH_JSON_PATHS[0])
    assert logged.is_set()
    assert [r.getMessage() for r in sqlglot_records] == ["another thread"]


def test_quote_identifier_doubles_quotes():
    assert quote_identifier("rules") == '"rules"'
    assert quote_identifier('we"ird') == '"we""ird"'
    check_select_only(
        f"SELECT * FROM {quote_identifier('update')}.{quote_identifier('t')}"
    )
