"""The SELECT-only guard: each check refuses on its own, and the two together.

The lexer and the parser are tested apart (``_lex`` / ``_parsed_problem``), so
removing either one fails a test here, and together (``check_select_only``)
against the bypasses found in the BigQuery guard (seq 13647) translated to
Redshift's readers.
"""

from __future__ import annotations

import logging

import pytest

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
    "unquoted-keyword-column": "SELECT update FROM t",
    "for-update": "SELECT * FROM t FOR UPDATE",
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
    "for-update",
    "unquoted-keyword-column",
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


def test_nothing_is_logged(caplog):
    caplog.set_level(logging.DEBUG)
    for sql in [*ADMITTED, *REFUSED.values()]:
        try:
            check_select_only(sql)
        except NotReadOnly:
            pass
    assert not [r for r in caplog.records if r.name.startswith("sqlglot")]
    assert "DROP" not in caplog.text


def test_quote_identifier_doubles_quotes():
    assert quote_identifier("rules") == '"rules"'
    assert quote_identifier('we"ird') == '"we""ird"'
    check_select_only(
        f"SELECT * FROM {quote_identifier('update')}.{quote_identifier('t')}"
    )
