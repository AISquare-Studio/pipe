"""SELECT-only: writes, DDL, scripts and several statements are refused before any call."""

from __future__ import annotations

import logging

import pytest
from aisquare.pipe.core.envelope import PullParams

from aisquare_pipe_snowflake import (
    NotReadOnly,
    SnowflakeQuerySource,
    SnowflakeToolkit,
    check_select_only,
    sqlguard,
)
from aisquare_pipe_snowflake.refs import TableRef
from aisquare_pipe_snowflake.sqlguard import MAX_SQL_CHARS, table_references
from tests.fakes import FakeSnowflake, blob

READ_ONLY = [
    "SELECT 1",
    "select id, body from policies.public.docs where id = 1",
    "SELECT v:a.b::string AS x FROM t",
    "SELECT 'a;b' || 'c' FROM t -- a comment; with a semicolon\n",
    "WITH c AS (SELECT 1 AS x) SELECT * FROM c",
    "SELECT f.value FROM t, LATERAL FLATTEN(input => t.v) f",
    "SELECT * FROM TABLE(FLATTEN(input => PARSE_JSON('[1]')))",
    "SELECT SEQ4() FROM TABLE(GENERATOR(ROWCOUNT => 10))",
    "SELECT * FROM TABLE(SPLIT_TO_TABLE('a,b', ','))",
    "SELECT * FROM t AT(OFFSET => -60)",
    "SELECT a FROM t MINUS SELECT a FROM u",
    "SELECT a FROM t UNION ALL SELECT a FROM u",
    "(SELECT 1)",
    'SELECT "update", "delete" FROM "My Db".s."T"',
    "SELECT * FROM t, LATERAL (SELECT 1)",
    "SELECT 'it''s' FROM t",
    "SELECT * FROM t ORDER BY a DESC LIMIT 10 OFFSET 5",
    "SELECT * FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1",
    "SELECT updated_at, created_by FROM t",
    "SELECT /* a block comment */ 1 // a line comment",
    "SELECT 1 -- a comment ended by CR LF\r\nFROM t // another\r\n",
    "SELECT 1 /* a * comment / with * and / */ FROM t",
]

REFUSED = {
    "insert": "INSERT INTO t VALUES (1)",
    "update": "UPDATE t SET a = 1",
    "delete": "DELETE FROM t",
    "merge": "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN DELETE",
    "truncate": "TRUNCATE TABLE t",
    "create": "CREATE TABLE x (a INT)",
    "ctas": "CREATE TABLE x AS SELECT 1",
    "drop": "DROP TABLE t",
    "alter": "ALTER TABLE t ADD COLUMN b INT",
    "undrop": "UNDROP TABLE t",
    "grant": "GRANT SELECT ON TABLE t TO ROLE r",
    "revoke": "REVOKE SELECT ON TABLE t FROM ROLE r",
    "copy": "COPY INTO t FROM @stage",
    "copy_unload": "COPY INTO @stage FROM (SELECT 1)",
    "put": "PUT file:///tmp/x @~",
    "remove": "REMOVE @stage",
    "use": "USE ROLE ACCOUNTADMIN",
    "set": "SET x = 1",
    "show": "SHOW TABLES",
    "describe": "DESCRIBE TABLE t",
    "call": "CALL p()",
    "execute_immediate": "EXECUTE IMMEDIATE 'DROP TABLE t'",
    "begin_block": "BEGIN SELECT 1; END",
    "declare": "DECLARE x INT; BEGIN RETURN 1; END",
    "transaction": "BEGIN TRANSACTION",
    "commit": "COMMIT",
    "two_selects": "SELECT 1; SELECT 2",
    "select_then_drop": "SELECT 1; DROP TABLE t",
    "two_semicolons": "SELECT 1;;",
    "with_insert": "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x",
    "select_into": "SELECT 1 INTO :v",
    "anonymous_procedure": (
        "WITH p AS PROCEDURE () RETURNS INT LANGUAGE SQL AS 'x' CALL p()"
    ),
    "procedure_as_table_function": "SELECT * FROM TABLE(my_proc(1))",
    "qualified_procedure": "SELECT * FROM TABLE(db.s.my_proc(1))",
    "lateral_udtf": "SELECT * FROM t, LATERAL my_udtf(t.a)",
    "quoted_table_function": 'SELECT * FROM TABLE("p"(1))',
    "result_scan": "SELECT * FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))",
    "flow_operator": "SELECT 1 ->> SELECT 2",
    "flow_operator_ddl": "SHOW TABLES ->> SELECT * FROM $1",
    "stage_columns": "SELECT $1 FROM @stage",
    "system_function": "SELECT SYSTEM$CANCEL_ALL_QUERIES(1)",
    "session_variable": "SELECT * FROM t WHERE a = $x",
    "dollar_string": "SELECT $$DROP TABLE t$$",
    "stage": "SELECT * FROM @mystage/file.csv",
    "bind_placeholder": "SELECT * FROM t WHERE a = ?",
    "bind_number": "SELECT * FROM t WHERE a = :1",
    "nextval": "SELECT seq.NEXTVAL",
    "for_update": "SELECT * FROM t FOR UPDATE",
    "backtick": "SELECT `a` FROM t",
    "escaped_quote_smuggle": "SELECT 'a\\';DROP TABLE t;--'",
    "comment_hides_nothing": "SELECT 1 /* unterminated",
    "unterminated_string": "SELECT 'abc",
    "unterminated_identifier": 'SELECT "abc FROM t',
    "empty": "",
    "only_whitespace": "   ",
    "only_comment": "-- nothing\n",
    "control_character": "SELECT 1\x00",
    # The BigQuery finding (manager note #13650), and its parts.
    "cr_and_nested_comments": (
        "SELECT 1 --\r/* /* */; DROP TABLE t; SELECT 1 /* */ -- */"
    ),
    "dash_comment_ended_by_cr": "SELECT 1 --\r; DROP TABLE t",
    "slash_comment_ended_by_cr": "SELECT 1 //\r; DROP TABLE t",
    "crlf_ends_the_comment": "SELECT 1 -- x\r\n; DROP TABLE t",
    "nested_block_comment": "SELECT 1 /* /* */ ; DROP TABLE t; /* */ */",
    "nested_by_shared_star": "SELECT 1 /* a /*/ FROM t",
}


@pytest.mark.parametrize("sql", READ_ONLY)
def test_one_read_only_query_is_allowed(sql):
    assert check_select_only(sql)


def test_one_trailing_semicolon_is_dropped():
    assert check_select_only("select 1;  ") == "select 1"
    assert check_select_only("SELECT 1; -- done\n") == "SELECT 1"


@pytest.mark.parametrize("sql", list(REFUSED.values()), ids=list(REFUSED))
def test_writes_ddl_scripts_and_several_statements_are_refused(sql):
    with pytest.raises(NotReadOnly):
        check_select_only(sql)


@pytest.mark.parametrize("sql", list(REFUSED.values()), ids=list(REFUSED))
def test_the_toolkit_refuses_them_before_any_request(sql):
    fake = FakeSnowflake()
    result = SnowflakeToolkit(session=fake).call_tool(
        "SNOWFLAKE_RUN_SELECT", {"statement": sql}, blob()
    )
    assert not result.successful
    assert result.error_code == "invalid_arguments", result.error
    assert fake.requests == []


@pytest.mark.parametrize("sql", list(REFUSED.values()), ids=list(REFUSED))
def test_the_source_refuses_them_before_any_request(sql):
    fake = FakeSnowflake()
    with pytest.raises(ValueError):
        list(
            SnowflakeQuerySource(session=fake).pull(
                blob(), PullParams({"sql": sql or " "})
            )
        )
    assert fake.requests == []


LEXER_CATCHES = [
    "SELECT 1 ->> SELECT 2",
    "SELECT $1 FROM @stage",
    "SELECT SYSTEM$CANCEL_ALL_QUERIES(1)",
    "SELECT * FROM TABLE(my_proc(1))",
    "SELECT * FROM t, LATERAL my_udtf(t.a)",
    "SELECT seq.NEXTVAL",
    "SELECT 1; DROP TABLE t",
    "SELECT * FROM t FOR UPDATE",
    "SELECT 'a\\';DROP TABLE t;--'",
]


@pytest.mark.parametrize("sql", LEXER_CATCHES)
def test_the_lexer_alone_refuses_what_the_parser_might_admit(sql, monkeypatch):
    monkeypatch.setattr(sqlguard, "_parsed_problem", lambda statement: None)
    with pytest.raises(NotReadOnly):
        check_select_only(sql)


PARSER_CATCHES = [
    "SELECT * FROM t WHERE a = ?",
    "SELECT * FROM t WHERE a = :1",
    "SELECT * FROM TABLE(my_proc(1))",
    "SELECT * FROM t, LATERAL my_udtf(t.a)",
    "SHOW TABLES",
    "DESCRIBE TABLE t",
    "INSERT INTO t SELECT 1",
    "SELECT 1; SELECT 2",
]


@pytest.mark.parametrize("sql", PARSER_CATCHES)
def test_the_parser_alone_refuses_what_the_lexer_might_admit(sql, monkeypatch):
    monkeypatch.setattr(sqlguard, "_lex", lambda text: sqlguard._Lexed(text, None))
    with pytest.raises(NotReadOnly):
        check_select_only(sql)


# A bare CR ends a line for some readers (sqlglot) and not for others, and
# some readers nest /* */ comments (sqlglot 25.3 to 26.24, for BigQuery):
# either one hides text from a reader that disagrees. The lexer reads a line
# comment to the first CR or LF, refuses one a bare CR ends, and refuses a /*
# inside a /* */ comment, whatever the parser makes of them.
@pytest.mark.parametrize(
    ("sql", "why"),
    [
        (
            "SELECT 1 --\r/* /* */; DROP TABLE t; SELECT 1 /* */ -- */",
            "bare CR",
        ),
        ("SELECT 1 --\r; DROP TABLE t", "bare CR"),
        ("SELECT 1 //\r; DROP TABLE t", "bare CR"),
        ("SELECT 1 -- x\r\n; DROP TABLE t", "more than one statement"),
        ("SELECT 1 /* /* */ ; DROP TABLE t; /* */ */", r"/\* inside a /\* \*/"),
        ("SELECT 1 /* a /*/ FROM t", r"/\* inside a /\* \*/"),
        ("SELECT 1 /* a */ /* b /* c */", r"/\* inside a /\* \*/"),
    ],
)
def test_the_lexer_alone_refuses_comments_readers_disagree_on(sql, why, monkeypatch):
    monkeypatch.setattr(sqlguard, "_parsed_problem", lambda statement: None)
    with pytest.raises(NotReadOnly, match=why):
        check_select_only(sql)


def test_a_line_comment_ended_by_cr_lf_hides_nothing():
    assert check_select_only("SELECT 1 -- note\r\n") == "SELECT 1 -- note"
    assert check_select_only("SELECT 1 // note\r\n, 2") == "SELECT 1 // note\r\n, 2"


def test_a_quote_escaped_with_a_backslash_reads_two_ways_and_is_refused():
    with pytest.raises(NotReadOnly, match="unterminated|two ways"):
        check_select_only("SELECT 'a\\'b' FROM t")
    assert check_select_only("SELECT 'a''b' FROM t")


def test_a_column_named_like_a_write_keyword_needs_double_quotes():
    with pytest.raises(NotReadOnly, match="names UPDATE"):
        check_select_only("SELECT update FROM t")
    assert check_select_only('SELECT "UPDATE" FROM t')


def test_the_message_never_echoes_the_sql():
    secretish = "SELECT secret_column_name FROM t; DROP TABLE very_private_table"
    with pytest.raises(NotReadOnly) as raised:
        check_select_only(secretish)
    assert "very_private_table" not in str(raised.value)
    assert "secret_column_name" not in str(raised.value)


def test_the_guard_never_logs_the_sql(caplog):
    caplog.set_level(logging.DEBUG)
    for sql in [
        "SELECT 1 FROM t WHERE a IN (SELECT x FROM private_t",
        "DROP TABLE private_t",
    ]:
        with pytest.raises(NotReadOnly):
            check_select_only(sql)
    assert "private_t" not in caplog.text


def test_a_statement_over_the_length_limit_is_refused():
    with pytest.raises(NotReadOnly, match="longer than"):
        check_select_only("SELECT 1" + " " * MAX_SQL_CHARS)


def test_table_references_leave_ctes_out_and_flag_what_cant_be_read():
    refs, complete = table_references(
        'WITH c AS (SELECT 1) SELECT * FROM c JOIN s.t ON 1 = 1 JOIN "My Db"."x"."Y" '
        "ON 1 = 1, docs",
        database="POL",
        schema="PUBLIC",
    )
    assert refs == [
        TableRef("POL", "S", "T"),
        TableRef("My Db", "x", "Y"),
        TableRef("POL", "PUBLIC", "DOCS"),
    ]
    assert complete
    assert table_references(
        "SELECT * FROM t, LATERAL FLATTEN(input => t.v) f",
        database="POL",
        schema="PUBLIC",
    ) == ([TableRef("POL", "PUBLIC", "T")], False)
    assert table_references(
        "SELECT * FROM POL.INFORMATION_SCHEMA.TABLES", database="POL", schema="PUBLIC"
    ) == ([], False)
    assert table_references("SELECT * FROM t", database=None, schema=None) == (
        [],
        False,
    )
