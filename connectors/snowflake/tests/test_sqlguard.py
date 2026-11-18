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
    # Inside a string, the forms the guard refuses are data.
    "SELECT 'price $5', '@home', 'AI_COMPLETE(x)', 'SNOWFLAKE.CORTEX.x', 'a!b' FROM t",
    # Backslash escapes that every reading ends at the same quote.
    "SELECT 'C:\\\\dir\\\\', 'tab\\tnew\\nline' FROM t",
    "SELECT * FROM t WHERE a != 1",
    "SELECT EXTRACT(year FROM TO_DATE(x)) FROM t",
    # Named like an AI function or CORTEX, but neither called nor a qualifier.
    'SELECT "AI_SCORE", ai_score, cortex, "!" FROM t',
    "SELECT TRANSLATE(name, 'ab', 'xy') FROM t",
    # Exponents, with a space before a sign that follows one.
    "SELECT 1e5 - 1, 1.5E-3, 2e+2, .5e1 FROM t -- 1e5\n",
]

# Reviewer seq 13859 M1. The lexer read a backslash as an escape in BOTH
# '...' and "..." or in NEITHER; Snowflake reads it in '...' only. In
# Snowflake's reading this is the string ''' with the alias "\", then the
# call, then a -- comment, and both old readings hid the call in quotes.
QUOTE_READING_REPRO = (
    "SELECT '\\'' \"\\\" , SYSTEM$CANCEL_ALL_QUERIES(CURRENT_SESSION()) -- \" /* ' */"
)


def _wrapped(form: str) -> str:
    """``form`` inside the M1 wrapper, hidden from both old readings."""
    return "SELECT '\\'' \"\\\" , " + form + " -- \" /* ' */"


QUOTE_READING_WRAPPED = {
    "wrapped_abort_session": _wrapped("SYSTEM$ABORT_SESSION(CURRENT_SESSION())"),
    "wrapped_user_stage": _wrapped("x FROM @~/f.csv"),
    "wrapped_named_stage": _wrapped("x FROM @db.s.st/f.csv"),
    "wrapped_dollar_string": _wrapped("$$x$$"),
    "wrapped_session_variable": _wrapped("$x"),
    "wrapped_cortex": _wrapped("SNOWFLAKE.CORTEX.COMPLETE('m', 'p')"),
    "wrapped_drop": "SELECT '\\'' \"\\\" ; DROP TABLE t ; SELECT 1 -- \" /* ' */",
}
# Reviewer seq 13859 m4, manager decision: Cortex AI functions bill AI
# credits, which no warehouse limit bounds. Refused, fail-closed.
CORTEX_CALLS = {
    "cortex_complete": "SELECT SNOWFLAKE.CORTEX.COMPLETE('mistral-large2', body) FROM t",
    "cortex_quoted": 'SELECT "SNOWFLAKE"."CORTEX"."COMPLETE"(\'m\', body) FROM t',
    "cortex_spaced": "SELECT SNOWFLAKE . /* c */ CORTEX . SENTIMENT(body) FROM t",
    "cortex_schema_only": "SELECT CORTEX.SUMMARIZE(body) FROM t",
    "cortex_in_where": "SELECT id FROM t WHERE SNOWFLAKE.CORTEX.SENTIMENT(body) > 0",
    "ai_complete": "SELECT AI_COMPLETE('mistral-large2', body) FROM t",
    "ai_quoted": "SELECT \"AI_COMPLETE\"('m', body) FROM t",
    "ai_lower_case": "SELECT ai_filter(body) FROM t",
    "ai_in_where": "SELECT id FROM t WHERE AI_FILTER(PROMPT('is {0} ok', body))",
    "ai_typed_in_sqlglot": "SELECT AI_CLASSIFY(body, ['a', 'b']) FROM t",
    "ai_aggregate": "SELECT AI_AGG(body, 'sum up') FROM t",
    "ai_spaced": "SELECT AI_EMBED /* c */ ('m', body) FROM t",
    # Found by a fuzz: a name can't start with a digit, so Snowflake (and
    # sqlglot) read 1AI_COMPLETE as the number 1, then AI_COMPLETE.
    "ai_after_a_digit": "SELECT 1AI_COMPLETE('m', body) FROM t",
    "cortex_after_a_digit": "SELECT 1CORTEX.COMPLETE('m', body) FROM t",
    # Reviewer seq 13917 b2: the same after an exponent (1e5, then AI_COMPLETE).
    "ai_after_an_exponent": "SELECT 1e5AI_COMPLETE('m', body) FROM t",
    "ai_after_a_decimal_exponent": "SELECT 1.5E3AI_COMPLETE('m', body) FROM t",
    "ai_after_a_signed_exponent": "SELECT 1e+5AI_COMPLETE('m', body) FROM t",
    "cortex_after_an_exponent": "SELECT 1e5CORTEX.COMPLETE('m', body) FROM t",
    "model_method": "SELECT m!PREDICT(body) FROM t",
    "qualified_model_method": "SELECT db.s.m ! PREDICT(body, 1) FROM t",
}
# M1 (b): a name holding $ that only quotes hide, or a string IDENTIFIER
# turns into a name.
DOLLAR_NAMES = {
    "quoted_system_function": 'SELECT "SYSTEM$ABORT_SESSION"(1)',
    "quoted_dollar_column": 'SELECT "a$b" FROM t',
    "identifier_system_function": "SELECT IDENTIFIER('SYSTEM$ABORT_SESSION')(1)",
    "identifier_cortex": "SELECT IDENTIFIER('SNOWFLAKE.CORTEX.COMPLETE')('m', 'p')",
    "identifier_column": "SELECT IDENTIFIER('c') FROM t",
}
# Reviewer seq 13859 m5: a qualified table function in a bare FROM or JOIN.
BARE_QUALIFIED_TABLE_FUNCTIONS = {
    "bare_from_qualified_flatten": "SELECT * FROM s.FLATTEN(input => t.v)",
    "bare_from_deep_qualified_flatten": "SELECT * FROM db.s.FLATTEN(input => t.v)",
    "bare_from_quoted_flatten": 'SELECT * FROM s."FLATTEN"(input => t.v)',
    "comma_join_qualified_flatten": "SELECT * FROM t, s.FLATTEN(input => t.v)",
    "join_qualified_flatten": "SELECT * FROM t JOIN s.FLATTEN(input => t.v) ON 1 = 1",
    "bare_from_qualified_generator": "SELECT * FROM s.GENERATOR(ROWCOUNT => 1)",
    # The same fuzz: TABLE( right after a digit.
    "table_call_after_a_digit": "SELECT * FROM t, 1TABLE(my_proc(1))",
    "table_call_after_an_exponent": "SELECT * FROM t, 1e5TABLE(my_proc(1))",
    "lateral_after_an_exponent": "SELECT * FROM t, 1e5LATERAL my_udtf(t.a)",
}
# Found by the quote fuzz once it had an exponent atom: sqlglot reads a sign
# after an exponent's digits (and an E with no digits after it) into the
# number. In 1e5-- '...' it sees 1e5-, then -, then a string, where Snowflake
# sees 1e5, then a comment, then the rest as code. Both checks admitted the
# first four at 8661c94.
NUMBER_SPLITS = {
    "exponent_sign_hides_a_udtf": "SELECT 1e5-- '\nFROM t, my_udtf(1) -- '",
    "bare_e_hides_a_udtf": "SELECT 1e-- '\nFROM t, my_udtf(1) -- '",
    "decimal_exponent_hides_a_udtf": "SELECT .5e5-- '\nFROM t, my_udtf(1) -- '",
    "exponent_sign_hides_a_bind": "SELECT 1e5-- '\nFROM t WHERE a = ? -- '",
    "exponent_minus_one": "SELECT 1e5-1",
    "e_runs_into_a_name": "SELECT 1ex FROM t",
}
# Reviewer seq 13917 a1: sqlglot reads -> / ->> as Postgres' JSON operators
# (SELECT 1 ->> DELETE FROM t parses as SELECT JSON_EXTRACT_PATH_TEXT(1,
# DELETE) FROM t), and in Snowflake ->> is the flow operator: the DELETE runs.
ARROW_OPERATORS = {
    "flow_operator_delete": "SELECT 1 ->> DELETE FROM t",
    "flow_operator_delete_where": "SELECT 1 ->> DELETE FROM t WHERE a = 1",
    "flow_operator_truncate": "SELECT 1 ->> TRUNCATE t",
    "flow_operator_spaced": "SELECT 1 /* c */ ->> /* c */ DELETE FROM t",
    "arrow_delete": "SELECT 1 -> DELETE FROM t",
    # -> also starts a Snowflake lambda: refused with it.
    "lambda": "SELECT FILTER(a, x -> x > 1) FROM t",
}

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
    # NEXTVAL, quoted or not: s."NEXTVAL" still takes a sequence's next value.
    "quoted_nextval": 'SELECT s."NEXTVAL"',
    "quoted_lowercase_nextval": 'SELECT s."nextval" FROM t',
    # Only an UNQUALIFIED built-in: s.FLATTEN is a user-defined function.
    "qualified_flatten": "SELECT * FROM TABLE(x.FLATTEN(input => PARSE_JSON('[1]')))",
    "deep_qualified_flatten": "SELECT * FROM TABLE(evil.s.FLATTEN(input => t.v))",
    "spaced_qualified_flatten": "SELECT * FROM TABLE(x /* c */ . FLATTEN(input => v))",
    "lateral_qualified_flatten": "SELECT * FROM t, LATERAL s.FLATTEN(input => t.v)",
    "qualified_generator": "SELECT * FROM TABLE(x.GENERATOR(ROWCOUNT => 1))",
    "quote_reading_repro": QUOTE_READING_REPRO,
    **QUOTE_READING_WRAPPED,
    **CORTEX_CALLS,
    **DOLLAR_NAMES,
    **BARE_QUALIFIED_TABLE_FUNCTIONS,
    **ARROW_OPERATORS,
    **NUMBER_SPLITS,
    # The lexer alone refuses these three (ONLY_THE_LEXER_REFUSES).
    "bare_write_word": "SELECT update FROM t",
    "backslash_quote_hidden_by_a_comment": "SELECT 'it\\'s' AS a -- '",
    "comment_ended_by_a_bare_cr": "SELECT 1 -- c\rFROM t",
    # The parser alone refuses these two (ONLY_THE_PARSER_REFUSES).
    "stage_in_a_string": "SELECT * FROM '@~/f.csv'",
    "named_stage_in_a_string": "SELECT * FROM '@st/f.csv' (FILE_FORMAT => 'ff')",
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
    'SELECT s."NEXTVAL"',
    'SELECT "NEXTVAL" FROM t',
    "SELECT 1; DROP TABLE t",
    "SELECT * FROM t FOR UPDATE",
    "SELECT 'a\\';DROP TABLE t;--'",
    "SELECT * FROM TABLE(x.FLATTEN(input => v))",
    "SELECT * FROM TABLE(evil.s.FLATTEN(input => v))",
    "SELECT * FROM t, LATERAL s.FLATTEN(input => t.v)",
    "SELECT * FROM TABLE(x.GENERATOR(ROWCOUNT => 1))",
    QUOTE_READING_REPRO,
    *QUOTE_READING_WRAPPED.values(),
    *CORTEX_CALLS.values(),
    *DOLLAR_NAMES.values(),
    # m5 in the lexer: a qualified call named like a built-in table function.
    # A bare FROM f(...) by any other name is the parser's alone (below).
    *BARE_QUALIFIED_TABLE_FUNCTIONS.values(),
    *ARROW_OPERATORS.values(),
    *NUMBER_SPLITS.values(),
]


@pytest.mark.parametrize("sql", LEXER_CATCHES)
def test_the_lexer_alone_refuses_what_the_parser_might_admit(sql, monkeypatch):
    monkeypatch.setattr(sqlguard, "_parsed_problem", lambda statement: None)
    with pytest.raises(NotReadOnly):
        check_select_only(sql)


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


# What the README and the module docstring say only the lexer refuses: a
# write keyword as a bare word (sqlglot reads a column), a control character,
# a second ";", and text readers disagree on (a backslash before a quote, a
# bare CR, nested comments).
ONLY_THE_LEXER_REFUSES = [
    "SELECT update FROM t",
    "SELECT 1\x00",
    "SELECT 1;;",
    "SELECT 'a\\';DROP TABLE t;--'",
    "SELECT 'it\\'s' AS a -- '",
    "SELECT 1 -- c\rFROM t",
    "SELECT 1 /* a /*/ FROM t",
]


@pytest.mark.parametrize("sql", ONLY_THE_LEXER_REFUSES)
def test_what_the_parser_leaves_to_the_lexer(sql):
    """If a parser rule starts catching one, drop it here and from both
    texts."""
    assert sqlguard._parsed_problem(sql) is None
    assert sqlguard._lex(sql).problem is not None


def test_every_form_only_the_lexer_refuses_is_listed():
    """a1 (seq 13917) was a write only the lexer refused, missing from the
    docs' list: a refused form the parser alone admits must be listed."""
    for sql in _unique([*REFUSED.values(), *LEXER_CATCHES]):
        if sqlguard._parsed_problem(sql) is None:
            assert sql in ONLY_THE_LEXER_REFUSES, sql


ONLY_THE_PARSER_REFUSES = [
    "SELECT * FROM t WHERE a = ?",
    "SELECT * FROM t WHERE a = :1",
    "SELECT * FROM my_udtf(1)",
    "SELECT * FROM t, my_udtf(t.a)",
    "SELECT * FROM t JOIN my_udtf(t.a) ON 1 = 1",
    # Reviewer seq 13917 b3: a stage named in a string. The lexer blanks it.
    "SELECT * FROM '@~/f.csv'",
    "SELECT * FROM '@st/f.csv' (FILE_FORMAT => 'ff')",
]

PARSER_CATCHES = _unique(
    [
        "SELECT * FROM t WHERE a = ?",
        "SELECT * FROM t WHERE a = :1",
        "SELECT * FROM TABLE(my_proc(1))",
        "SELECT * FROM t, LATERAL my_udtf(t.a)",
        "SHOW TABLES",
        "DESCRIBE TABLE t",
        "INSERT INTO t SELECT 1",
        "SELECT 1; SELECT 2",
        "SELECT seq.NEXTVAL",
        "SELECT seq.nextval FROM t",
        'SELECT s."NEXTVAL"',
        'SELECT "NEXTVAL" FROM t',
        "SELECT NEXTVAL()",
        "SELECT * FROM TABLE(x.FLATTEN(input => v))",
        "SELECT * FROM TABLE(evil.s.FLATTEN(input => v))",
        "SELECT * FROM t, LATERAL s.FLATTEN(input => t.v)",
        "SELECT * FROM TABLE(x.GENERATOR(ROWCOUNT => 1))",
        # M1 (b): the parser alone refuses $ names, stages and $$ strings.
        QUOTE_READING_REPRO,
        *QUOTE_READING_WRAPPED.values(),
        "SELECT SYSTEM$CANCEL_ALL_QUERIES(1)",
        "SELECT a$b FROM t",
        "SELECT $1 FROM @stage",
        "SELECT * FROM t WHERE a = $x",
        "SELECT $$DROP TABLE t$$",
        "SELECT * FROM @mystage/file.csv",
        "SELECT x FROM @~/f.csv",
        "SELECT * FROM @%t",
        *CORTEX_CALLS.values(),
        *DOLLAR_NAMES.values(),
        *BARE_QUALIFIED_TABLE_FUNCTIONS.values(),
        *ARROW_OPERATORS.values(),
        *NUMBER_SPLITS.values(),
        *ONLY_THE_PARSER_REFUSES,
    ]
)


@pytest.mark.parametrize("sql", PARSER_CATCHES)
def test_the_parser_alone_refuses_what_the_lexer_might_admit(sql, monkeypatch):
    monkeypatch.setattr(sqlguard, "_lex", lambda text: sqlguard._Lexed(text, None))
    with pytest.raises(NotReadOnly):
        check_select_only(sql)


@pytest.mark.parametrize("sql", ONLY_THE_PARSER_REFUSES)
def test_what_the_lexer_leaves_to_the_parser(sql, monkeypatch):
    """The exceptions the README and the module docstring name: the lexer
    can't tell a bare FROM's table function from EXTRACT(... FROM f(x)), and
    has no rule for binds. If a lexer rule starts catching one, move it to
    LEXER_CATCHES and update both texts."""
    assert sqlguard._lex(sql).problem is None
    monkeypatch.setattr(sqlguard, "_parsed_problem", lambda statement: None)
    assert check_select_only(sql) == sql


def test_every_form_only_the_parser_refuses_is_listed():
    for sql in _unique([*REFUSED.values(), *PARSER_CATCHES]):
        if sqlguard._lex(sql).problem is None:
            assert sql in ONLY_THE_PARSER_REFUSES, sql


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
    with pytest.raises(NotReadOnly, match="more than one way"):
        check_select_only("SELECT 'a\\'b' FROM t")
    assert check_select_only("SELECT 'a''b' FROM t")


@pytest.mark.parametrize(
    "sql",
    [
        # Reviewer seq 13917 b1. Every reading found no problem and ended the
        # statement in the same place, so these passed the lexer, though the
        # readings put the quotes in different places.
        "SELECT 'it\\'s' AS a -- '",
        "SELECT X'\\'' AS h FROM t, my_udtf(1) -- '",
        _wrapped("1"),  # the M1 wrapper around a harmless 1
    ],
)
def test_every_reading_must_blank_the_same_spans(sql):
    assert sqlguard._lex(sql).problem == sqlguard._MORE_THAN_ONE_WAY
    with pytest.raises(NotReadOnly, match="more than one way"):
        check_select_only(sql)


@pytest.mark.parametrize(
    "sql",
    [
        # Snowflake: an identifier named a\ (no escape in "..."), then FROM t.
        'SELECT 1 AS "a\\" FROM t',
        # Snowflake: the string a'b. A reader without escapes ends it early.
        "SELECT 'a\\'b' FROM t",
        # Snowflake: the string x\' (an escaped backslash, an escaped quote).
        "SELECT 'x\\\\\\'' FROM t",
    ],
)
def test_quoting_that_reads_more_than_one_way_is_refused(sql):
    with pytest.raises(NotReadOnly, match="more than one way"):
        check_select_only(sql)


def test_the_lexer_reads_quotes_as_snowflake_does():
    """M1 (a): Snowflake's reading (a backslash escapes in '...' only)
    decides, so it sees the call that both old readings hid. The other three
    readings must agree with it."""
    assert sqlguard._READINGS[0] == (True, False)
    assert set(sqlguard._READINGS) == {
        (strings, names) for strings in (True, False) for names in (True, False)
    }
    blanked = sqlguard._blank(
        QUOTE_READING_REPRO, escapes_in_strings=True, escapes_in_names=False
    )
    assert "SYSTEM$CANCEL_ALL_QUERIES(CURRENT_SESSION())" in blanked
    assert sqlguard._lex(QUOTE_READING_REPRO).problem == sqlguard._DOLLAR


@pytest.mark.parametrize("schema", ["CORTEX", "cortex", '"CORTEX"'])
def test_run_select_refuses_the_default_schema_cortex(schema):
    """m4: with SNOWFLAKE.CORTEX as the default, an unqualified COMPLETE(...)
    is SNOWFLAKE.CORTEX.COMPLETE."""
    fake = FakeSnowflake()
    result = SnowflakeToolkit(session=fake).call_tool(
        "SNOWFLAKE_RUN_SELECT",
        {
            "statement": "SELECT COMPLETE('m', 'p')",
            "database": "SNOWFLAKE",
            "schema_name": schema,
        },
        blob(),
    )
    assert not result.successful
    assert result.error_code == "invalid_arguments", result.error
    assert "CORTEX" in result.error
    configured = SnowflakeToolkit(session=fake).call_tool(
        "SNOWFLAKE_RUN_SELECT",
        {"statement": "SELECT COMPLETE('m', 'p')"},
        blob(database="SNOWFLAKE", schema="CORTEX"),
    )
    assert configured.error_code == "invalid_arguments", configured.error
    assert fake.requests == []


def test_a_sql_pull_refuses_the_default_schema_cortex():
    fake = FakeSnowflake()
    source = SnowflakeQuerySource(session=fake)
    with pytest.raises(ValueError, match="CORTEX"):
        list(
            source.pull(
                blob(),
                PullParams({"sql": "SELECT COMPLETE('m', 'p')", "schema": "cortex"}),
            )
        )
    with pytest.raises(ValueError, match="CORTEX"):
        list(
            source.pull(
                blob(database="SNOWFLAKE", schema="CORTEX"),
                PullParams({"sql": "SELECT COMPLETE('m', 'p')"}),
            )
        )
    assert fake.requests == []


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


# sqlglot logs a JSON path it can't read ("Invalid JSON path syntax. ...
# <the path>") at WARNING, and any key with a dash is one: these statements
# are valid Snowflake, and the guard ADMITS them.
ADMITTED_WITH_DASHED_JSON_PATHS = [
    "SELECT GET_PATH(record, 'member.ssn-123-45-6789') FROM claims",
    "SELECT GET_PATH(v, 'a-private_1') FROM t",
    "SELECT v:'patient-4111111111111111' FROM t",
    "SELECT v:'x-private_2' AS x FROM t",
    "SELECT JSON_EXTRACT_PATH_TEXT(v, 'dx-private_3') FROM t",
]
# The lexer passes these and the parser refuses them, after the JSON path.
REFUSED_WITH_DASHED_JSON_PATHS = [
    "SELECT GET_PATH(v, 'a-private_4') FROM t WHERE a = ?",
    "SELECT v:'x-private_5' FROM t WHERE a = :1",
]


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


def test_the_guard_never_logs_the_sql(caplog, sqlglot_records):
    for sql in [
        "SELECT 1 FROM t WHERE a IN (SELECT x FROM private_t",
        "DROP TABLE private_t",
        *REFUSED_WITH_DASHED_JSON_PATHS,
    ]:
        with pytest.raises(NotReadOnly):
            check_select_only(sql)
    for sql in ADMITTED_WITH_DASHED_JSON_PATHS:
        assert check_select_only(sql) == sql
        table_references(sql, database="POL", schema="PUBLIC")
    for secret in ("private", "ssn", "123-45-6789", "4111111111111111"):
        assert secret not in caplog.text
    assert [r.getMessage() for r in sqlglot_records] == []


def test_sqlglot_still_logs_outside_the_guard(sqlglot_records):
    """The guard quiets sqlglot only while it parses: no global switch."""
    import sqlglot

    check_select_only(ADMITTED_WITH_DASHED_JSON_PATHS[0])
    sqlglot.parse_one(
        "SELECT GET_PATH(v, 'outside-the-guard') FROM t", read="snowflake"
    )
    logging.getLogger("sqlglot").warning("after the guard")
    messages = [r.getMessage() for r in sqlglot_records]
    assert any("outside-the-guard" in m for m in messages), messages
    assert "after the guard" in messages
    assert logging.root.manager.disable == logging.NOTSET


def test_the_guard_stays_quiet_if_its_filter_was_removed(sqlglot_records):
    """A host that resets the "sqlglot" logger's filters doesn't bring the
    leak back: the guard re-attaches its own on the next parse."""
    logging.getLogger("sqlglot").filters.clear()
    assert check_select_only(ADMITTED_WITH_DASHED_JSON_PATHS[0])
    assert sqlglot_records == []


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


# Reviewer seq 13859: the earlier quote fuzz missed M1 because its alphabet
# had no comment characters, and comments bring two wrong readings back into
# step. This one has them, and checks the lexer alone against sqlglot's
# tokenizer, which reads quotes and comments as Snowflake does. It found
# "1AI_COMPLETE(": a name can't start with a digit, so that is 1, then a call.
_FUZZ_ATOMS = [
    *("'", '"', "\\", "''", '""'),
    *("--", "//", "/*", "*/", "\n", "\r"),
    *(";", " ", ",", "(", ")", "x", "1", "1e5", "!", "$", "@"),
    *("SYSTEM$X()", "DROP TABLE t", "AI_C(1)", "CORTEX.F(1)"),
]


def test_what_the_lexer_admits_sqlglot_reads_the_same_way():
    import random

    from sqlglot.tokens import TokenType

    rng = random.Random(13859)
    admitted = 0
    for _ in range(50_000):
        atoms = rng.randint(1, 14)
        sql = "SELECT 1 , " + "".join(rng.choice(_FUZZ_ATOMS) for _ in range(atoms))
        lexed = sqlguard._lex(sql)
        if lexed.problem is not None:
            continue
        admitted += 1
        try:
            tokens = sqlguard._DIALECT.tokenize(lexed.statement)
        except Exception:  # noqa: BLE001, S112 — sqlglot can't read it: the parser refuses
            continue
        for index, token in enumerate(tokens):
            kind, text = token.token_type, token.text
            following = (
                tokens[index + 1].token_type if index + 1 < len(tokens) else None
            )
            assert kind not in (TokenType.SEMICOLON, TokenType.PARAMETER), sql
            assert kind != TokenType.RAW_STRING, sql  # $$...$$
            assert text.upper() != "DROP", sql
            if kind in (TokenType.STRING, TokenType.IDENTIFIER):
                continue  # a literal's value, or a quoted name not called
            assert "$" not in text and text != "!", sql
            assert not (
                text.upper().startswith("AI_") and following == TokenType.L_PAREN
            )
            assert not (text.upper() == "CORTEX" and following == TokenType.DOT), sql
    assert admitted > 5_000  # the alphabet still reaches the checks
