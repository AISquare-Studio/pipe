"""Every SQL guard parses through core's parse_statements, at the one floor
the pyproject declares (#46 review seq 14088 m2).

A guard used to repeat the same sequence itself: the floor check, a quiet
tokenize and parse, and any exception a refusal; and each kept its own copy
of the sqlglot floor, so raising the pyproject's floor would have left the
guards admitting the old one. Now :func:`aisquare.pipe.core.sql.parse_statements`
does all of it and :data:`~aisquare.pipe.core.sql.SQLGLOT_FLOOR` is the only
floor. These tests read every connector module that imports sqlglot (its
AST, nothing is imported) so a new guard can't skip either.
"""

from __future__ import annotations

import ast
import importlib.util
import logging
import sys
from pathlib import Path

import pytest

from aisquare.pipe.core import sql as core_sql
from aisquare.pipe.core.sql import (
    SQLGLOT_FLOOR,
    SqlglotTooOld,
    Unparsable,
    parse_statements,
    sqlglot_version,
)

ROOT = Path(__file__).resolve().parent.parent
#: sqlglot entry points that parse or tokenize: a guard calls none of them.
_PARSING_CALLS = frozenset(
    {"tokenize", "parse", "parse_one", "parse_into", "transpile"}
)


def _dep_floor():
    spec = importlib.util.spec_from_file_location(
        "dep_floor_for_test", ROOT / "scripts" / "dep_floor.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up
    spec.loader.exec_module(module)
    return module


def _imports_sqlglot(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            a.name == "sqlglot" or a.name.startswith("sqlglot.") for a in node.names
        ):
            return True
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and (node.module == "sqlglot" or node.module.startswith("sqlglot."))
        ):
            return True
    return False


def _sqlglot_modules() -> dict[str, ast.Module]:
    found = {}
    for path in sorted(ROOT.glob("connectors/*/src/**/*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if _imports_sqlglot(tree):
            found[str(path.relative_to(ROOT))] = tree
    return found


def test_the_floor_is_the_one_dep_floor_reads_from_the_pyproject():
    floor = _dep_floor().floor(ROOT, "sqlglot")
    assert SQLGLOT_FLOOR == sqlglot_version(floor.version), (
        f"core.sql.SQLGLOT_FLOOR is {SQLGLOT_FLOOR}, the pyproject's extras say "
        f"{floor.specifier}: raise both together"
    )


def test_the_known_guards_are_found():
    found = _sqlglot_modules()
    for guard in (
        "connectors/bigquery/src/aisquare_pipe_bigquery/sqlguard.py",
        "connectors/snowflake/src/aisquare_pipe_snowflake/sqlguard.py",
        "connectors/aws/src/aisquare_pipe_aws/redshift/sqlguard.py",
    ):
        assert guard in found


@pytest.mark.parametrize("module", sorted(_sqlglot_modules()))
def test_every_module_importing_sqlglot_parses_through_parse_statements(module):
    tree = _sqlglot_modules()[module]
    imported = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "aisquare.pipe.core.sql"
        for alias in node.names
    }
    assert "parse_statements" in imported, (
        f"{module} imports sqlglot, not parse_statements"
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )
            assert name not in _PARSING_CALLS, (
                f"{module}:{node.lineno} calls {name}(): parse with parse_statements"
            )
            assert name != "sqlglot_at_least", (
                f"{module}:{node.lineno} checks the floor itself: parse_statements does"
            )
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                text = ast.unparse(base)
                assert not text.endswith("Parser"), (
                    f"{module}:{node.lineno} subclasses {text}: parse_statements' "
                    "parser is the quiet one"
                )


@pytest.mark.parametrize("module", sorted(_sqlglot_modules()))
def test_no_guard_keeps_its_own_floor(module):
    for node in ast.walk(_sqlglot_modules()[module]):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            assert not (
                isinstance(target, ast.Name) and target.id == "SQLGLOT_FLOOR"
            ), f"{module}:{node.lineno} sets its own SQLGLOT_FLOOR: import core's"


# -- parse_statements itself ------------------------------------------------------------


@pytest.fixture
def bigquery():
    pytest.importorskip("sqlglot")
    from sqlglot.dialects.bigquery import BigQuery

    return BigQuery()


def test_it_parses_and_returns_tokens_and_statements(bigquery):
    parsed = parse_statements("SELECT 1; SELECT 2", bigquery)
    assert [s.key for s in parsed.statements] == ["select", "select"]
    assert parsed.tokens and parsed.tokens[0].text.upper() == "SELECT"


def test_an_older_sqlglot_than_the_floor_refuses(bigquery, monkeypatch):
    import sqlglot

    monkeypatch.setattr(sqlglot, "__version__", "26.24.0")
    with pytest.raises(SqlglotTooOld, match="needs sqlglot 26.25 or later"):
        parse_statements("SELECT 1", bigquery)
    with pytest.raises(SqlglotTooOld, match="needs sqlglot 99.1 or later"):
        parse_statements("SELECT 1", bigquery, floor=(99, 1))
    monkeypatch.setattr(sqlglot, "__version__", "unknown")
    with pytest.raises(SqlglotTooOld):
        parse_statements("SELECT 1", bigquery)
    # The default is read when called: core's one floor.
    monkeypatch.setattr(sqlglot, "__version__", "26.25.0")
    monkeypatch.setattr(core_sql, "SQLGLOT_FLOOR", (26, 26))
    with pytest.raises(SqlglotTooOld, match="26.26"):
        parse_statements("SELECT 1", bigquery)


def test_any_exception_while_parsing_refuses_with_no_sql(bigquery, monkeypatch):
    with pytest.raises(Unparsable) as raised:
        parse_statements("SELECT FROM WHERE secret_column_7", bigquery)
    assert "secret_column_7" not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__

    def broken(self, *args, **kwargs):
        raise KeyError("secret_column_7")  # a bug in sqlglot, not a ParseError

    monkeypatch.setattr(core_sql.quiet_parser_class(bigquery), "parse", broken)
    with pytest.raises(Unparsable):
        parse_statements("SELECT 1", bigquery)


def test_nothing_sqlglot_logs_while_it_parses_gets_out(bigquery, caplog):
    caplog.set_level(logging.DEBUG, logger="sqlglot")
    # A JSON path with a dash: sqlglot logs "Invalid JSON path syntax" with it.
    parse_statements("SELECT JSON_EXTRACT(p, '$.member.ssn-123') FROM t", bigquery)
    assert not [r for r in caplog.records if r.name.startswith("sqlglot")]
    assert "ssn-123" not in caplog.text


def test_the_quiet_parser_is_made_once_per_dialect(bigquery):
    from sqlglot.dialects.bigquery import BigQuery

    assert core_sql.quiet_parser_class(bigquery) is core_sql.quiet_parser_class(
        BigQuery()
    )
    assert issubclass(core_sql.quiet_parser_class(bigquery), BigQuery.Parser)
