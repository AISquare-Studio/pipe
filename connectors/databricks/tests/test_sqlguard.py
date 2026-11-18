"""SELECT-only SQL: each check on its own, and both together."""

from __future__ import annotations

import pytest

from aisquare_pipe_databricks import sqlguard
from aisquare_pipe_databricks.sqlguard import NotReadOnly, check_select_only

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
