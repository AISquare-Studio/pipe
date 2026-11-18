"""SELECT-only SQL: each check on its own, and both together."""

from __future__ import annotations

import itertools
import logging
import random
import re

import pytest

from aisquare_pipe_databricks import DatabricksToolkit, sqlguard
from aisquare_pipe_databricks.sqlguard import NotReadOnly, check_select_only
from tests.fakes import FakeDatabricks, pat_blob

READS = [
    "SELECT 1",
    "select a, b from main.policies.docs where body like '%; drop table x%' limit 10",
    "WITH recent AS (SELECT * FROM main.p.docs WHERE updated > now() - INTERVAL 7 DAYS) SELECT * FROM recent",
    "(SELECT 1)",
    "SELECT 1 UNION ALL SELECT 2",
    "SELECT * FROM a JOIN b USING (id) -- set aside\n",
    'SELECT `select`, "x;y" FROM t',
    "SELECT * FROM read_files('/Volumes/main/p/docs/')",
]
WRITES = [
    "SELECT 1; DROP TABLE t",
    "INSERT INTO t SELECT 1",
    "INSERT OVERWRITE t SELECT 1",
    "UPDATE t SET a = 1",
    "DELETE FROM t",
    "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN DELETE",
    "CREATE TABLE x AS SELECT 1",
    "CREATE OR REPLACE VIEW v AS SELECT 1",
    "DROP TABLE t",
    "ALTER TABLE t ADD COLUMN c INT",
    "TRUNCATE TABLE t",
    "SELECT * INTO x FROM t",
    "WITH a AS (SELECT 1) INSERT INTO t SELECT * FROM a",
    "SET spark.sql.ansi.enabled = false",
    "USE CATALOG main",
    "OPTIMIZE t ZORDER BY (a)",
    "VACUUM t",
    "COPY INTO t FROM '/Volumes/x'",
    "GRANT SELECT ON TABLE t TO `x`",
    "CACHE TABLE t",
    "REFRESH TABLE t",
    "DESCRIBE t",
    'select "a\\" ; delete from t; --"',
    "SELECT 1 /* never closed",
    "SELECT 'unterminated",
    "",
    "   ",
]


@pytest.mark.parametrize("sql", READS)
def test_a_read_passes_both_checks(sql):
    assert check_select_only(sql) == sql.strip()
    assert sqlguard._parsed(sql) is None and sqlguard._lexical(sql) is None


@pytest.mark.parametrize("sql", WRITES)
def test_writes_ddl_and_multiple_statements_are_refused(sql):
    with pytest.raises(NotReadOnly, match="only one SELECT|is empty"):
        check_select_only(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; DROP TABLE t",
        "INSERT INTO t SELECT 1",
        "DELETE FROM t",
        "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN DELETE",
        "CREATE TABLE x AS SELECT 1",
        "WITH a AS (SELECT 1) INSERT INTO t SELECT * FROM a",
        "SELECT * INTO x FROM t",
        "SET spark.x = 1",
        "USE CATALOG main",
        "DROP TABLE t",
    ],
)
def test_the_parser_alone_refuses_them(sql):
    """The parser is a check of its own, not a backup the lexer hides."""
    assert sqlguard._parsed(sql) is not None


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; DROP TABLE t",
        "INSERT INTO t SELECT 1",
        "SELECT * INTO x FROM t",
        "SELECT 1 # drop",
    ],
)
def test_the_lexer_alone_refuses_them(sql):
    assert sqlguard._lexical(sql) is not None


def test_overlong_sql_is_refused():
    with pytest.raises(NotReadOnly, match="longer than"):
        check_select_only("SELECT " + "1," * sqlguard.MAX_SQL_CHARS + "1")


# -- seq 13947: Spark's own reading ------------------------------------------------
#
# Spark (SqlBaseLexer.g4): a -- comment runs past a backslash-LF and ends at
# CR or LF; block comments nest; backslash escapes in '...' and "..." but not
# in backticks. Each payload hides a DROP from a reader that disagrees.

#: The reopen's payloads (python literals as given), and their parts.
REOPEN_PAYLOADS = {
    # FAIL 1: Spark reads a comment, DROP TABLE t, a comment.
    "backslash_lf_continues_the_comment": "-- \\\nSELECT 1, ' \nDROP TABLE t -- '",
    # FAIL 2: Spark nests: a comment, then DROP TABLE t.
    "nested_comment_hides_the_drop": "/* /* */ SELECT 1, ' */ DROP TABLE t -- '",
    # FAIL 3.
    "dash_comment_ended_by_cr": "SELECT 1 --\r; DROP TABLE t",
    "nested_comment_then_a_string": "SELECT 1 /* /* */ ' */ ; DROP TABLE t; -- '",
    "backslash_in_quotes_not_backticks": "SELECT '\\'' `\\` ; DROP TABLE t ; -- ` /* ' */",
    "backslash_lf_then_a_string": "SELECT 1 -- \\\n' \n; DROP TABLE t; -- '",
}


@pytest.mark.parametrize("sql", REOPEN_PAYLOADS.values(), ids=REOPEN_PAYLOADS)
def test_the_lexer_alone_refuses_the_reopen_payloads(sql):
    assert sqlguard._lexical(sql) is not None
    with pytest.raises(NotReadOnly):
        check_select_only(sql)


@pytest.mark.parametrize("sql", REOPEN_PAYLOADS.values(), ids=REOPEN_PAYLOADS)
def test_the_parser_alone_refuses_the_reopen_payloads(sql):
    """FAIL 1 included: at 810abb2 only the lexer refused it (seq 13990 m1)."""
    assert sqlguard._parsed(sql) is not None


@pytest.mark.parametrize(
    ("sql", "why"),
    [
        ("SELECT 1 -- \\\nFROM t", "ends in a backslash"),
        ("SELECT 1 -- \\\r\nFROM t", "ends in a backslash"),
        ("SELECT 1 --\\\n, 2", "ends in a backslash"),
        ("SELECT 1 -- x\r, 2", "bare CR"),
        ("SELECT 1 /* a /* b */ c */", r"/\* inside a /\* \*/"),
        ("SELECT 1 /* a /*/ FROM t", r"/\* inside a /\* \*/"),
        ("SELECT 1 /* a /*+ b */", r"/\* inside a /\* \*/"),
        ("SELECT 1 /* never closed", "unterminated"),
        ("SELECT 'a\\'b' FROM t", "backslash in a string"),
        ('SELECT "a\\"b" FROM t', "backslash in a string"),
        ("SELECT `a\\` FROM t", "backslash in a string"),
        ("SELECT r'\\' FROM t", "backslash in a string"),
    ],
)
def test_the_lexer_refuses_where_readers_disagree(sql, why):
    assert re.search(why, sqlguard._lexical(sql) or "")


def test_line_comments_end_at_cr_lf_and_hide_nothing():
    assert (
        check_select_only("SELECT 1 -- note\r\nFROM t") == "SELECT 1 -- note\r\nFROM t"
    )
    assert check_select_only("SELECT 1 -- a \\ in a comment\nFROM t")
    assert check_select_only("SELECT 1 -- a backslash, then a space \\ \nFROM t")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 -- \\\nFROM t",
        "SELECT 1 -- \\\r\nFROM t",
        "SELECT 1 -- ends the text \\",
        "SELECT 1 /* a \\*/ FROM t",
        "SELECT 1 /* a\nb \\*/",
    ],
)
def test_no_comment_may_end_in_a_backslash_in_either_layer(sql):
    """Spark carries a -- comment on past a backslash right before its LF.
    sqlglot's reading of a comment doesn't say where it ended, so the parser
    refuses any comment that ends in a backslash, and the lexer the same,
    either kind, at a line break or at the end of the text."""
    assert "ends in a backslash" in (sqlguard._lexical(sql) or "")
    assert "ends in a backslash" in (sqlguard._parsed(sql) or "")


def test_quotes_read_one_way_whatever_the_reader():
    for sql in (
        'SELECT \'it\'\'s\', "a ""b""", `c``d` FROM t',
        "SELECT 'C:\\\\dir\\\\', r'\\d+', '\\n' FROM t",
        'SELECT "it\'s" FROM t',
    ):
        assert check_select_only(sql) == sql


#: Where sqlglot's reading and Spark's differ, the parser refuses on its own
#: (seq 13990 m1: at 810abb2 the lexer alone refused the first two).
THE_PARSER_ALONE_REFUSES = [
    (REOPEN_PAYLOADS["backslash_lf_continues_the_comment"], "ends in a backslash"),
    ("SELECT `a` -- \\\n, `b` FROM t", "ends in a backslash"),
    ("SELECT '${env:HOME}'", r"\$\{"),
    ("SELECT 1 -- ${x}", r"\$\{"),
    # Spark reads a hint's insides as tokens wherever it stands: here ';' and
    # DROP. sqlglot reads a /*+ that is not right after SELECT as a comment.
    ("SELECT 1 /*+ ; DROP x */", "not right after SELECT"),
    ("SELECT 1 /*+ DELETE */", "not right after SELECT"),
    # sqlglot nests a /*+ inside a comment, Spark doesn't: Spark's comment ends
    # at the first */, then ; DROP TABLE t.
    ("SELECT 1 /* a /*+ */ ; DROP TABLE t -- */", "not right after SELECT"),
    # A hint's insides beyond names and numbers: Spark reads the string first,
    # so its */ doesn't end the hint (then ; DROP TABLE t); sqlglot ends it there.
    ("SELECT /*+ x('*/' ; DROP TABLE t ; --') 1", "holds more than names"),
    # sqlglot reads {# #} as a (Jinja) comment in every dialect.
    ("SELECT 1 {# ; DROP TABLE t; #}", r"\{#"),
    # Spark reads */ as one token, so */* opens no comment: ; DROP TABLE t.
    ("SELECT 2 */* ; DROP TABLE t */ 3", r"\*/ outside a comment"),
    # Under spark.sql.ansi.doubleQuotedIdentifiers, "..." takes no escapes:
    # the identifier "\" ends at its second quote.
    ('SELECT "\\" ; DROP TABLE t ; --"', "backslash in a"),
    ("SELECT ınsert FROM t", "non-ASCII"),
]


@pytest.mark.parametrize(("sql", "why"), THE_PARSER_ALONE_REFUSES)
def test_the_parser_alone_refuses_where_sqlglot_reads_unlike_spark(sql, why):
    assert re.search(why, sqlguard._parsed(sql) or ""), sqlguard._parsed(sql)
    assert sqlguard._lexical(sql) is not None  # and so does the lexer


def test_a_hint_right_after_select_reads_alike_and_passes():
    for sql in (
        "SELECT /*+ BROADCAST(t) */ * FROM t",
        "SELECT /*+ REPARTITION(8, c), COALESCE(2) */ c FROM t",
        "SELECT * FROM (SELECT /*+ BROADCAST(t) */ * FROM t)",
        "SELECT '/*+ not a hint */', `{#x#}` FROM t",
    ):
        assert check_select_only(sql) == sql


# The two layers are composed: each catches what the other admits. A
# check_select_only that dropped either one fails here. None of these is a
# write in Spark's reading: each layer refuses every write on its own (the
# fuzzes below run each layer alone).
ONLY_THE_LEXER_REFUSES = [
    "SELECT $1",  # sqlglot reads $ as a parameter; Databricks $$...$$ as text
    "SELECT a$b FROM t",
    "SELECT 1 /* a /* b */ c */",  # Spark and sqlglot nest; other readers don't
    "SELECT 1 -- x\r, 2",  # Spark and sqlglot end it at the bare CR
    "SELECT 'a\\'b' FROM t",  # Spark and sqlglot take the \' escape
    "SELECT 'a\u2028b'",  # an LS, in a string
]
ONLY_THE_PARSER_REFUSES = [
    "SELECT * FROM t FOR SHARE",  # a row lock: no write word to see
    "SELECT (",
    "SELECT * FROM t WHERE",
    "SELECT /*+ SKEW('t') */ * FROM t",  # a hint with a string in it
]


@pytest.mark.parametrize("sql", ONLY_THE_LEXER_REFUSES)
def test_what_only_the_lexer_refuses_is_refused(sql):
    assert sqlguard._parsed(sql) is None, "the parser admits it: that is the point"
    assert sqlguard._lexical(sql) is not None
    with pytest.raises(NotReadOnly):
        check_select_only(sql)


@pytest.mark.parametrize("sql", ONLY_THE_LEXER_REFUSES + ONLY_THE_PARSER_REFUSES)
def test_what_only_one_layer_refuses_is_no_write_in_sparks_reading(sql):
    assert not _spark_sees_a_write(sql)


@pytest.mark.parametrize("sql", ONLY_THE_PARSER_REFUSES)
def test_what_only_the_parser_refuses_is_refused(sql):
    assert sqlguard._lexical(sql) is None, "the lexer admits it: that is the point"
    assert sqlguard._parsed(sql) is not None
    with pytest.raises(NotReadOnly):
        check_select_only(sql)


@pytest.mark.parametrize("layer", ["_lexical", "_parsed"])
def test_dropping_either_layer_lets_something_through(layer, monkeypatch):
    """The mutation the tester ran (seq 13947 FAIL 3): with one layer
    admitting everything, the other layer's only-catches get through, so
    the two tests above are what pin the composition."""
    monkeypatch.setattr(sqlguard, layer, lambda sql: None)
    caught_by_it = (
        ONLY_THE_LEXER_REFUSES if layer == "_lexical" else ONLY_THE_PARSER_REFUSES
    )
    for sql in caught_by_it:
        assert check_select_only(sql)


def test_check_select_only_runs_both_layers(monkeypatch):
    calls = []
    monkeypatch.setattr(sqlguard, "_lexical", lambda sql: calls.append("lexer"))
    monkeypatch.setattr(sqlguard, "_parsed", lambda sql: calls.append("parser"))
    check_select_only("SELECT 1")
    assert calls == ["lexer", "parser"]


SIDE_EFFECTS = [
    "SELECT * FROM list_secrets()",
    "SELECT * FROM `list_secrets`()",
    "SELECT * FROM vector_search(index => 'main.p.idx', query_text => 'x', num_results => 5)",
    "SELECT http_request(conn => 'c', method => 'POST', path => '/x')",
    "SELECT * FROM remote_query('c', query => 'DELETE FROM x')",
    "SELECT reflect('java.lang.Thread', 'sleep', 1000L)",
    "SELECT java_method('java.lang.Thread', 'sleep', 1000L)",
    "SELECT try_reflect('a', 'b')",
    "SELECT system.builtin.reflect('a', 'b')",
    "SELECT `reflect`('a', 'b')",
    "SELECT `system`.`builtin`.`reflect`('a', 'b')",
    "SELECT reflect /* c */ ('a', 'b')",
    "SELECT secret('scope', 'key')",
    "SELECT try_secret('scope', 'key')",
    "SELECT ai_query('endpoint', body) FROM t",
    "SELECT AI_SUMMARIZE(body) FROM t",
    "SELECT * FROM IDENTIFIER('main.p.docs')",
    "SELECT TRANSFORM(a) USING 'cat' AS (b) FROM t",
    "SELECT TRANSFORM(a) USING r'cat' FROM t",
]


@pytest.mark.parametrize("sql", SIDE_EFFECTS)
def test_side_effect_calls_are_refused_by_each_layer(sql):
    assert sqlguard._lexical(sql) is not None
    assert sqlguard._parsed(sql) is not None


@pytest.mark.parametrize(
    ("sql", "label"),
    [
        ("SELECT ai_member_ssn_123456789(x) FROM t", "AI_* function"),
        ("SELECT `ai_member_ssn_123456789`(x) FROM t", "AI_* function"),
        ("SELECT `system`.`builtin`.`ReFlect`('a', 'b')", "REFLECT"),
        ("SELECT * FROM List_Secrets()", "LIST_SECRETS"),
    ],
)
def test_a_refused_call_is_named_by_its_canonical_name_only(sql, label):
    """The caller's spelling is caller text (seq 13990 n2)."""
    for message in (sqlguard._lexical(sql), sqlguard._parsed(sql)):
        assert message is not None and message.endswith(label), message
        assert "SSN" not in message.upper() and "ReFlect" not in message


def test_names_like_side_effect_functions_are_fine_when_not_called():
    sql = "SELECT secret, ai_score, identifier, transform(a, x -> x + 1) FROM t"
    assert check_select_only(sql) == sql


@pytest.mark.parametrize(
    ("sql", "why"),
    [
        ("SELECT '${spark.sql.x}'", r"\$\{"),
        ("SELECT 1 -- ${x}", r"\$\{"),
        ("SELECT ınsert FROM t", "non-ASCII"),  # Spark 3 upper-cases ı to I
        ("SELECT 1 FROM t\u00a0", "non-ASCII"),
        ("SELECT 1\x0b FROM t", "control character"),
        ("SELECT 1\x00", "control character"),
        ("SELECT 'a\u2028b'", "NEL, LS, PS"),
        ("SELECT 1 \\ 2", "backslash outside quotes"),
        ("SELECT $1", r"a \$ outside quotes"),
        ("SELECT $$x$$", r"a \$ outside quotes"),
        ("SELECT x FROM t WHERE y = $p", r"a \$ outside quotes"),
        ("SELECT /*+ $x */ 1", r"a \$ outside quotes"),
        ("SELECT /*+ x */* ; DROP TABLE t; */ 1", "more than one statement"),
    ],
)
def test_the_lexer_refuses_what_spark_reads_another_way(sql, why):
    assert re.search(why, sqlguard._lexical(sql) or "")


def test_a_dollar_in_strings_and_comments_is_fine():
    sql = "SELECT '$5' AS price, `cost$` FROM t -- costs $5"
    assert check_select_only(sql) == sql


def test_non_ascii_text_in_strings_and_comments_is_fine():
    sql = "SELECT 'Zoë Ünal' AS `naïve` FROM t -- café"
    assert check_select_only(sql) == sql


def test_a_hint_is_read_as_tokens():
    assert check_select_only("SELECT /*+ BROADCAST(t) */ * FROM t")
    assert "names DELETE" in (sqlguard._lexical("SELECT /*+ DELETE */ 1") or "")


def test_below_the_sqlglot_floor_nothing_runs(monkeypatch):
    """sqlglot 25.0 to 25.2 don't nest Spark's block comments (FAIL 2): a host
    with an older sqlglot than the extra's floor runs nothing."""
    import sqlglot

    monkeypatch.setattr(sqlglot, "__version__", "25.0.0")
    with pytest.raises(NotReadOnly, match="needs sqlglot 26.25"):
        check_select_only("SELECT 1")
    monkeypatch.setattr(sqlglot, "__version__", "26.25.0")
    assert check_select_only("SELECT 1")


# -- no SQL in logs (seq 13947 FAIL 4) ---------------------------------------------

#: Valid Databricks SQL the guard ADMITS, whose JSON path sqlglot can't read:
#: it logs "Invalid JSON path syntax. ... <the path>" at WARNING.
ADMITTED_WITH_DASHED_JSON_PATHS = [
    "SELECT get_json_object(payload, '$.member.ssn-123-45-6789') FROM claims",
    "SELECT get_json_object(v, '$.dob-1970') FROM t",
]


@pytest.fixture
def sqlglot_records(caplog):
    """Every record the "sqlglot" logger emits, at DEBUG and up."""
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
    for sql in ADMITTED_WITH_DASHED_JSON_PATHS:
        assert check_select_only(sql) == sql
    for sql in (
        "SELECT 1 FROM t WHERE a IN (SELECT x FROM private_t",
        "DROP TABLE private_t",
    ):
        with pytest.raises(NotReadOnly):
            check_select_only(sql)
    for secret in ("ssn", "123-45-6789", "1970", "private_t"):
        assert secret not in caplog.text
    assert [r.getMessage() for r in sqlglot_records] == []


def test_the_toolkit_logs_no_sql_either(caplog, sqlglot_records):
    fake = FakeDatabricks()
    result = DatabricksToolkit(session=fake).call_tool(
        "DATABRICKS_RUN_SELECT",
        {"statement": ADMITTED_WITH_DASHED_JSON_PATHS[0]},
        pat_blob(),
    )
    assert result.successful, result.error
    assert "123-45-6789" not in caplog.text
    assert sqlglot_records == []


def test_sqlglot_still_logs_outside_the_guard(sqlglot_records):
    """Quiet only while the guard parses: nothing is switched off globally."""
    import sqlglot

    check_select_only(ADMITTED_WITH_DASHED_JSON_PATHS[0])
    sqlglot.parse_one(
        "SELECT get_json_object(v, '$.outside-the-guard') FROM t", read="databricks"
    )
    assert any("outside-the-guard" in r.getMessage() for r in sqlglot_records)
    assert logging.root.manager.disable == logging.NOTSET


# -- the reopen payloads never reach Databricks ------------------------------------


@pytest.mark.parametrize(
    "sql",
    [*REOPEN_PAYLOADS.values(), *SIDE_EFFECTS],
    ids=[*REOPEN_PAYLOADS, *range(len(SIDE_EFFECTS))],
)
def test_run_select_refuses_them_with_no_request(sql):
    fake = FakeDatabricks()
    result = DatabricksToolkit(session=fake).call_tool(
        "DATABRICKS_RUN_SELECT", {"statement": sql}, pat_blob()
    )
    assert not result.successful and result.error_code == "forbidden"
    assert fake.requests == []


# -- a differential fuzz against Spark's own reading -------------------------------


def spark_tokens(sql: str) -> list[str] | None:
    """The tokens Spark's lexer (SqlBaseLexer.g4) reads in ``sql``, upper-cased,
    comments dropped; None when Spark can't lex it. Only the rules that decide
    where tokens end: SIMPLE_COMMENT ('--' ('\\\\\\n' | ~[\\r\\n])* '\\r'? '\\n'?),
    nested BRACKETED_COMMENT (not '/*+'), HINT_START/HINT_END, STRING_LITERAL
    (backslash escapes, '' or adjacent literals; R'...' raw), DOUBLEQUOTED_STRING,
    BACKQUOTED_IDENTIFIER ('``', no escapes), IDENTIFIER runs, one-char tokens."""
    tokens: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if sql.startswith("--", i):
            j = i + 2
            while j < n:
                if sql.startswith("\\\n", j):
                    j += 2
                elif sql[j] in "\r\n":
                    break
                else:
                    j += 1
            i = j + (1 if sql.startswith("\r", j) else 0)
            i += 1 if sql.startswith("\n", i) else 0
        elif sql.startswith("/*+", i):
            tokens.append("/*+")
            i += 3
        elif sql.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if sql.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                elif sql.startswith("/*", j) and not sql.startswith("/*+", j):
                    depth, j = depth + 1, j + 2
                else:
                    j += 1
            if depth:
                return None
            i = j
        elif sql.startswith("*/", i):
            tokens.append("*/")
            i += 2
        elif c in "'\"`" or (
            c in "rR"
            and sql[i + 1 : i + 2] in ("'", '"')
            and not (i and (sql[i - 1].isalnum() or sql[i - 1] == "_"))
        ):
            raw = c in "rR"
            q = sql[i + 1] if raw else c
            j = i + 2 if raw else i + 1
            while True:
                if j >= n:
                    return None
                if sql[j] == "\\" and q != "`" and not raw:
                    j += 2
                    continue
                if sql[j] == q:
                    if q == "`" and sql.startswith("``", j):
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            tokens.append("<quoted>")
            i = j
        elif c.isalnum() or c == "_":
            j = i
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            tokens.append(sql[i:j].upper())
            i = j
        elif c in " \t\r\n":
            i += 1
        else:
            tokens.append(c)
            i += 1
    return tokens


PIECES = [
    "'",
    '"',
    "`",
    "\\",
    "--",
    "/*",
    "*/",
    "/*+",
    "\n",
    "\r",
    "r",
    " ; DROP x ",
    " ",
]
#: For the longer random strings: fewer pieces, and the FAIL 1 opener whole.
LONG_PIECES = ["'", "`", '"', "\\", "--", "/*", "*/", "\n", ";DROP x", "--\\\n"]


def _spark_sees_a_write(sql: str) -> bool:
    tokens = spark_tokens(sql)
    if tokens is None:
        return False  # Spark refuses to read it at all
    after = tokens[tokens.index(";") :] if ";" in tokens else []
    return "DROP" in tokens or set(after) - {";"} != set()


def test_whatever_the_lexer_admits_spark_reads_as_one_statement_without_a_write():
    """Every "SELECT 1 " + up to four pieces from the quote, comment and
    escape alphabet, and 400,000 seeded random strings of five to eight
    longer pieces: whatever the lexer admits, Spark's reading has no DROP
    and nothing after a ';'. (The parser is not consulted.) With this seed
    the fuzz also catches each of these lexer mutations on its own: a --
    comment ended at LF only, a hint read as a comment, ``*/`` not read as one
    token, no refusal of a backslash-LF in a -- comment, no refusal of a
    nested /*, and quotes read only with (or only without) backslash
    escapes."""
    checked = admitted = 0
    for count in range(1, 5):
        for pieces in itertools.product(PIECES, repeat=count):
            sql = "SELECT 1 " + "".join(pieces)
            checked += 1
            if sqlguard._lexical(sql) is None:
                admitted += 1
                assert not _spark_sees_a_write(sql), repr(sql)
    rng = random.Random(34)
    for _ in range(400_000):
        count = rng.randint(5, 8)
        sql = "SELECT 1 " + "".join(rng.choice(LONG_PIECES) for _ in range(count))
        checked += 1
        if sqlguard._lexical(sql) is None:
            admitted += 1
            assert not _spark_sees_a_write(sql), repr(sql)
    assert checked > 430_000 and admitted > 10_000


#: The parser's alphabet: the lexer's, plus what only sqlglot reads apart
#: (``{#``, a bare ``*`` before ``/``) and CRLF, a hint that passes, and a
#: string holding ``*/``.
PARSER_PIECES = [*PIECES, "*", "{#", "#}", "\r\n", "'*/'", "/*+ x */"]
PARSER_LONG_PIECES = [*LONG_PIECES, "*", "{#", "/*+", "r", "\r"]


def test_whatever_the_parser_admits_spark_reads_as_one_statement_without_a_write():
    """The parser ALONE (the lexer is not consulted), on every "SELECT 1 " +
    up to four pieces of its alphabet and 200,000 seeded random strings of
    five to eight longer pieces: whatever Spark's reading finds a write in
    (a DROP, or anything after a ';'), the parser refuses. At 810abb2 it
    admitted the FAIL 1 shape, a /*+ hint not right after SELECT, a /*+
    nested in a comment, {# #} and */* (seq 13990 m1)."""
    checked = writes = 0
    for count in range(1, 5):
        for pieces in itertools.product(PARSER_PIECES, repeat=count):
            sql = "SELECT 1 " + "".join(pieces)
            checked += 1
            if _spark_sees_a_write(sql):
                writes += 1
                assert sqlguard._parsed(sql) is not None, repr(sql)
    rng = random.Random(1399)
    for _ in range(200_000):
        count = rng.randint(5, 8)
        sql = "SELECT 1 " + "".join(
            rng.choice(PARSER_LONG_PIECES) for _ in range(count)
        )
        checked += 1
        if _spark_sees_a_write(sql):
            writes += 1
            assert sqlguard._parsed(sql) is not None, repr(sql)
    assert checked > 300_000 and writes > 20_000
