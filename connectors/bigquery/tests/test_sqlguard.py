"""The SELECT-only guard: what runs, what is refused before ANY request, and
that each of its two checks refuses on its own."""

from __future__ import annotations

import logging

import pytest
from aisquare.pipe.core.envelope import PullParams

from aisquare_pipe_bigquery import (
    BigQueryQuerySource,
    BigQueryToolkit,
    NotReadOnly,
    sqlguard,
)
from aisquare_pipe_bigquery.sqlguard import check_select_only, table_references
from tests.fakes import FakeGoogle, blob

ALLOWED = [
    "SELECT 1",
    "select id, title FROM `acme-analytics.policies.documents` WHERE active",
    "WITH recent AS (SELECT * FROM policies.documents) SELECT COUNT(*) FROM recent",
    "(SELECT 1) UNION ALL (SELECT 2)",
    "SELECT * EXCEPT (body) REPLACE (UPPER(title) AS title) FROM policies.documents",
    (
        "SELECT * FROM policies.documents FOR SYSTEM_TIME AS OF "
        "TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 HOUR)"
    ),
    "SELECT CASE WHEN x > 1 THEN 'big' ELSE 'small' END FROM UNNEST([1, 2]) AS x",
    "SELECT 'DROP TABLE x; DELETE FROM y' AS harmless_text",
    "SELECT '''multi\nline; INSERT''' AS s, r'\\d+;' AS pattern",
    "SELECT `update`, `set` FROM policies.documents  # the columns are named so",
    "SELECT 1 -- a comment; DROP TABLE x\n",
    "SELECT REPEAT('a', 3), IF(TRUE, 1, 2)",
    # GoogleSQL escapes are always on: \' does not close the string, so this
    # is ONE string literal, as BigQuery reads it.
    "SELECT 'a\\'; DROP TABLE policies.documents; --'",
    # Windows (CR LF) and old Mac (bare CR) line ends end a comment as LF does.
    "SELECT id,\r\n  title -- the name\r\nFROM policies.documents\r\n",
    "SELECT 1 # a bare CR ends me\r, 2",
    "SELECT '''multi\r\nline''' AS s",
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_one_read_only_query_is_allowed(sql):
    assert check_select_only(sql) == sql.strip()


def test_one_trailing_semicolon_is_dropped():
    assert check_select_only("SELECT 1;") == "SELECT 1"
    assert check_select_only("SELECT 1 ;  -- done\n") == "SELECT 1"


# The bypass #37 was reopened for (tester seq 13647), and its two halves.
CR_DASH = "SELECT 1 --\r; DROP TABLE ds.t"
CR_POUND = "SELECT 1 #\r; DROP TABLE ds.t"
NESTED = "SELECT 1 /* /* */ ; DELETE FROM t WHERE TRUE /* */"
CHAINED = "SELECT 1 --\r/* /* */; DROP TABLE ds.t; SELECT 1 /* */ -- */"

REFUSED = {
    # DML
    "insert": "INSERT INTO policies.documents (id) VALUES (1)",
    "update": "UPDATE policies.documents SET active = FALSE WHERE TRUE",
    "delete": "DELETE FROM policies.documents WHERE TRUE",
    "merge": "MERGE policies.documents t USING policies.staging s ON t.id = s.id "
    "WHEN MATCHED THEN DELETE",
    "truncate": "TRUNCATE TABLE policies.documents",
    # DDL / DCL
    "create": "CREATE TABLE policies.copy AS SELECT * FROM policies.documents",
    "create_temp_function": "CREATE TEMP FUNCTION f() AS (1)",
    "drop": "DROP TABLE policies.documents",
    "alter": "ALTER TABLE policies.documents SET OPTIONS (description = 'x')",
    "undrop": "UNDROP SCHEMA policies",
    "grant": "GRANT `roles/bigquery.dataViewer` ON TABLE policies.documents "
    "TO 'user:x@example.com'",
    "export": "EXPORT DATA OPTIONS (uri = 'gs://b/*.csv', format = 'CSV') AS SELECT 1",
    "load": "LOAD DATA INTO policies.documents FROM FILES (uris = ['gs://b/x'])",
    # scripting
    "declare": "DECLARE x INT64",
    "set": "SET @@dataset_project_id = 'other'",
    "begin_block": "BEGIN SELECT 1; END",
    "transaction": "BEGIN TRANSACTION",
    "call": "CALL policies.proc()",
    "execute_immediate": "EXECUTE IMMEDIATE 'DELETE FROM policies.documents WHERE TRUE'",
    "assert": "ASSERT (SELECT COUNT(*) FROM policies.documents) > 0",
    "raise": "RAISE USING MESSAGE = 'x'",
    "loop": "LOOP SELECT 1; END LOOP",
    # several statements
    "two_selects": "SELECT 1; SELECT 2",
    "select_then_delete": "SELECT 1; DELETE FROM policies.documents WHERE TRUE",
    "declare_then_select": "DECLARE x INT64 DEFAULT 1; SELECT x",
    "temp_function_then_select": "CREATE TEMP FUNCTION f() AS (1); SELECT f()",
    # \\ is an escaped backslash, so the string closes and DELETE is a
    # second statement.
    "escaped_quote_smuggle": "SELECT 'a\\\\'; DELETE FROM t WHERE TRUE; --'",
    "comment_hides_nothing": "SELECT 1 /* ; */ ; DELETE FROM t WHERE TRUE",
    # BigQuery ends -- and # comments at a bare CR, so DROP is a second statement.
    "cr_ends_dash_comment": CR_DASH,
    "cr_ends_pound_comment": CR_POUND,
    # GoogleSQL comments don't nest: '/* /* */' is one comment, and DELETE runs.
    "nested_comment": NESTED,
    "nested_comment_overlapping_closer": "SELECT 1 /* a /*/ ; DELETE FROM t WHERE TRUE /* */",
    # Both chained: BigQuery reads SELECT 1; DROP TABLE ds.t; SELECT 1.
    "cr_then_nested_comment": CHAINED,
    # A one-line string or backtick identifier can't hold a CR (or LF).
    "cr_in_string": "SELECT 'a\r'; DELETE FROM t WHERE TRUE; --'",
    "cr_in_double_quoted_string": 'SELECT "a\r"; DELETE FROM t WHERE TRUE; --"',
    "cr_in_backtick_identifier": "SELECT `a\r` FROM t",
    "lf_in_backtick_identifier": "SELECT `a\n` FROM t",
    # a write inside a read
    "select_into": "SELECT 1 INTO x",
    # not SQL a query can be
    "empty": "   ",
    "only_comment": "-- nothing\n",
    "unterminated_string": "SELECT 'oops",
    "unterminated_comment": "SELECT 1 /* never closed",
}


@pytest.mark.parametrize("sql", list(REFUSED.values()), ids=list(REFUSED))
def test_dml_ddl_scripting_and_several_statements_are_refused(sql):
    with pytest.raises(NotReadOnly, match="only one SELECT|empty"):
        check_select_only(sql)


@pytest.mark.parametrize("sql", list(REFUSED.values()), ids=list(REFUSED))
def test_the_toolkit_refuses_them_before_any_request(sql):
    """Not even a token is minted: the guard runs before the client exists."""
    google = FakeGoogle()
    result = BigQueryToolkit(session=google).call_tool(
        "BIGQUERY_RUN_SELECT", {"sql": sql}, blob()
    )
    assert not result.successful and result.error_code == "invalid_arguments"
    assert google.requests == []


@pytest.mark.parametrize(
    "sql",
    [
        REFUSED["delete"],
        REFUSED["select_then_delete"],
        REFUSED["execute_immediate"],
        REFUSED["create"],
        CR_DASH,
        CR_POUND,
        NESTED,
        CHAINED,
    ],
)
def test_the_source_refuses_them_before_any_request(sql):
    google = FakeGoogle()
    with pytest.raises(NotReadOnly):
        list(BigQueryQuerySource(session=google).pull(blob(), PullParams({"sql": sql})))
    assert google.requests == []


LAYER_CASES = [
    REFUSED["delete"],
    REFUSED["select_then_delete"],
    REFUSED["declare"],
    REFUSED["select_into"],
    REFUSED["merge"],
    REFUSED["execute_immediate"],
    REFUSED["assert"],
    REFUSED["escaped_quote_smuggle"],
    CR_DASH,
    CR_POUND,
    NESTED,
    CHAINED,
]


def test_the_lexer_alone_refuses_what_the_parser_might_admit(monkeypatch):
    """Each check is enough on its own: with the parser switched off (it
    admits everything), check_select_only still refuses writes, scripts and
    second statements."""
    monkeypatch.setattr(sqlguard, "_parsed_problem", lambda sql: None)
    for sql in LAYER_CASES:
        with pytest.raises(NotReadOnly):
            check_select_only(sql)
    assert check_select_only("SELECT 1") == "SELECT 1"


def test_the_parser_alone_refuses_what_the_lexer_might_admit(monkeypatch):
    """And with the lexer switched off, the parser refuses them too."""
    monkeypatch.setattr(
        sqlguard, "_lex", lambda sql: sqlguard._Lexed(sql.strip().rstrip(";"), None)
    )
    for sql in LAYER_CASES:
        with pytest.raises(NotReadOnly):
            check_select_only(sql)
    assert check_select_only("SELECT 1") == "SELECT 1"


def test_the_lexer_reads_line_ends_and_comments_as_bigquery_does(monkeypatch):
    """ZetaSQL ends -- and # comments at a CR or an LF, a bare CR included;
    its one-line strings and backtick identifiers hold neither; its /* */
    comments don't nest. The lexer alone (the parser off) refuses each way of
    hiding text there, and says why."""
    monkeypatch.setattr(sqlguard, "_parsed_problem", lambda sql: None)
    for sql in (CR_DASH, CR_POUND):
        with pytest.raises(NotReadOnly, match="more than one statement"):
            check_select_only(sql)
    for sql in (NESTED, CHAINED, REFUSED["nested_comment_overlapping_closer"]):
        with pytest.raises(NotReadOnly, match="don't nest"):
            check_select_only(sql)
    for key in (
        "cr_in_string",
        "cr_in_double_quoted_string",
        "cr_in_backtick_identifier",
        "lf_in_backtick_identifier",
    ):
        with pytest.raises(NotReadOnly, match="runs past a line end"):
            check_select_only(REFUSED[key])
    # A bare CR is a line end like LF: what follows is SQL again.
    assert check_select_only("SELECT 1 --\r, 2") == "SELECT 1 --\r, 2"
    # A backslash escapes a CR LF pair as one (still one string).
    assert check_select_only("SELECT 'a\\\r\nb'") == "SELECT 'a\\\r\nb'"
    assert check_select_only("SELECT 1 /* a */ /**/") == "SELECT 1 /* a */ /**/"


def test_the_installed_sqlglot_reads_bigquery_comments_flat():
    """sqlglot 25.3 to 26.24 read BigQuery's /* */ comments as nested, so its
    parse alone admitted CHAINED. The [bigquery] extra's floor is 26.25, and
    CI runs this suite at that floor as well as at the newest
    (scripts/dep_floor.py in validate.yml)."""
    tokens = sqlguard._DIALECT.tokenize("SELECT 1 /* /* */ AS x")
    assert [t.text for t in tokens] == ["SELECT", "1", "AS", "x"]


def test_a_column_named_like_a_write_keyword_needs_backticks():
    with pytest.raises(NotReadOnly, match="names UPDATE .*backticks"):
        check_select_only("SELECT update FROM policies.documents")
    assert check_select_only("SELECT `update` FROM policies.documents")


def test_the_message_never_echoes_the_sql():
    secret = "DELETE FROM payroll.salaries WHERE name = 'Jane Roe'"
    with pytest.raises(NotReadOnly) as raised:
        check_select_only(secret)
    assert "payroll" not in str(raised.value) and "Jane" not in str(raised.value)


def test_the_guard_never_logs_the_sql(caplog):
    """sqlglot logs "'<sql>' contains unsupported syntax" for statements it
    falls back on; the guard's parser has that switched off."""
    caplog.set_level(logging.DEBUG)
    for sql in (
        "EXECUTE IMMEDIATE 'SELECT secret_column FROM payroll.salaries'",
        "CALL payroll.give_raise()",
        "BEGIN SELECT secret_column FROM payroll.salaries; END",
    ):
        with pytest.raises(NotReadOnly):
            check_select_only(sql)
    assert "payroll" not in caplog.text and "secret_column" not in caplog.text


def test_a_statement_over_the_length_limit_is_refused():
    with pytest.raises(NotReadOnly, match="longer than"):
        check_select_only("SELECT 1 " + " " * sqlguard.MAX_SQL_CHARS)


def test_table_references_leave_ctes_out_and_flag_what_cant_be_read():
    refs, complete = table_references(
        "WITH recent AS (SELECT * FROM policies.documents) "
        "SELECT * FROM recent JOIN `other-proj.hr.people` USING (id)"
    )
    assert {(r.project, r.dataset, r.table) for r in refs} == {
        (None, "policies", "documents"),
        ("other-proj", "hr", "people"),
    }
    assert complete
    for sql in (
        "SELECT * FROM policies.events_* WHERE _TABLE_SUFFIX > '2026'",
        "SELECT * FROM `region-us`.INFORMATION_SCHEMA.TABLES",
        "SELECT * FROM documents",
    ):
        assert table_references(sql)[1] is False, sql
    refs, complete = table_references(
        "SELECT * FROM documents", default_dataset=(None, "policies")
    )
    assert complete and (refs[0].dataset, refs[0].table) == ("policies", "documents")
