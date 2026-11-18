"""Every SQL guard parses through core's parse_statements, at the one floor
the pyproject declares (#46 review seq 14088 m2).

A guard used to repeat the same sequence itself: the floor check, a quiet
tokenize and parse, and any exception a refusal; and each kept its own copy
of the sqlglot floor, so raising the pyproject's floor would have left the
guards admitting the old one. Now :func:`aisquare.pipe.core.sql.parse_statements`
does all of it and :data:`~aisquare.pipe.core.sql.SQLGLOT_FLOOR` is the only
floor. These tests read every connector module that imports sqlglot (its
AST, nothing is imported) so a new guard can't skip either.

What counts as parsing outside parse_statements: sqlglot's parsing
primitives (``_PARSING_NAMES``, under any name), its module-level string
builders (``_BUILDER_NAMES``: ``exp.condition(sql)`` parses its string, and
logs as it does, #50 review seq 14161 n2) and the modules that parse what
they are handed (``_PARSING_MODULES``: ``sqlglot.optimizer.optimize(sql)``),
those two where a guard reaches them through sqlglot. An Expression's own
builder methods (``.where()``, ``.select()``, ``.join()``, ...) parse a
string too, and are not listed (their names are everyday words): a guard
reads the trees parse_statements gives it and builds no SQL; review keeps
it so.
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
_CORE_SQL = "aisquare.pipe.core.sql"
#: What parses or tokenizes in sqlglot, or makes what does (a dialect's
#: ``parser()``/``tokenizer``, their classes, the modules that hold them): a
#: guard references none of them, called or not, under any alias.
_PARSING_NAMES = frozenset(
    {
        "tokenize",
        "parse",
        "parse_one",
        "parse_into",
        "maybe_parse",
        "transpile",
        "parser",
        "tokenizer",
        "parser_class",
        "tokenizer_class",
        "Parser",
        "Tokenizer",
    }
)
#: sqlglot's module-level builders that parse a string argument (through
#: ``maybe_parse``), outside parse_statements and its quiet logging (#50
#: review seq 14161 n2). Flagged where a guard reaches one through sqlglot
#: (``exp.condition``, ``exp.DataType.build``, ``from sqlglot.expressions
#: import select``, ``getattr(exp, "func")``), not as a bare local name: they
#: are everyday words.
_BUILDER_NAMES = frozenset(
    {
        "alias_",
        "and_",
        "array",
        "build",
        "case",
        "cast",
        "column_table_names",
        "condition",
        "delete",
        "except_",
        "expand",
        "from_",
        "func",
        "insert",
        "intersect",
        "merge",
        "not_",
        "or_",
        "paren",
        "parse_identifier",
        "replace_placeholders",
        "replace_tables",
        "select",
        "subquery",
        "table_name",
        "to_column",
        "to_interval",
        "to_table",
        "tuple_",
        "union",
        "update",
        "xor",
    }
)
#: sqlglot's modules whose entry points parse a string they are handed
#: (``optimizer.optimize(sql)``, ``executor.execute(sql)``, ...): a guard
#: reaches none of them.
_PARSING_MODULES = frozenset({"optimizer", "executor", "planner", "lineage", "diff"})
#: core.sql's own pieces of what parse_statements does: a guard that
#: references one is doing that part itself.
_CORE_ONLY = frozenset({"sqlglot_at_least", "quiet_parser_class"})
_FLOOR = "SQLGLOT_FLOOR"
_ATTRIBUTE_CALLS = ("getattr", "setattr", "hasattr")


def _dep_floor():
    spec = importlib.util.spec_from_file_location(
        "dep_floor_for_test", ROOT / "scripts" / "dep_floor.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up
    spec.loader.exec_module(module)
    return module


def _is_sqlglot(module: str | None) -> bool:
    return bool(module) and (module == "sqlglot" or module.startswith("sqlglot."))


def _called_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return getattr(func, "id", "")


def _constant_arg(node: ast.Call, index: int) -> object:
    if len(node.args) > index and isinstance(node.args[index], ast.Constant):
        return node.args[index].value
    return None


def _imports_sqlglot(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            _is_sqlglot(a.name) for a in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and _is_sqlglot(node.module):
            return True
        # importlib.import_module("sqlglot..."), __import__("sqlglot...")
        if isinstance(node, ast.Call) and _called_name(node) in (
            "import_module",
            "__import__",
        ):
            name = _constant_arg(node, 0)
            if isinstance(name, str) and _is_sqlglot(name):
                return True
    return False


def _from_imports(tree: ast.Module) -> dict[str, tuple[str, str]]:
    """Each name a ``from X import Y [as Z]`` binds: ``Z -> (X, Y)``."""
    bound = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                bound[alias.asname or alias.name] = (node.module, alias.name)
    return bound


def _module_aliases(tree: ast.Module) -> dict[str, str]:
    """Each name an ``import X [as Z]`` or a ``from P import M`` binds, to
    the dotted path it names (``import a.b`` binds ``a``)."""
    bound = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bound[alias.asname] = alias.name
                else:
                    head = alias.name.split(".", 1)[0]
                    bound[head] = head
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                bound[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return bound


def _resolved(name: str, imports: dict[str, tuple[str, str]]) -> str:
    """The name ``name`` was imported as (``po`` -> ``parse_one``), else itself."""
    return imports[name][1] if name in imports else name


def _sqlglot_path(node: ast.AST, modules: dict[str, str]) -> str | None:
    """The dotted sqlglot path ``node`` (a name or an attribute chain) reaches
    through the module's imports (``exp.DataType`` ->
    ``sqlglot.exp.DataType``); None when it isn't sqlglot's."""
    if not isinstance(node, (ast.Name, ast.Attribute)):
        return None
    head, _, rest = ast.unparse(node).partition(".")
    if head not in modules:
        return None
    path = ".".join(filter(None, (modules[head], rest)))
    return path if _is_sqlglot(path) else None


def _parsing_module(path: str) -> bool:
    """``path`` (dotted, sqlglot's) is in or under one of _PARSING_MODULES."""
    return bool(_PARSING_MODULES & set(path.split(".")[1:]))


def _uses_parse_statements(tree: ast.Module) -> bool:
    """``from aisquare.pipe.core.sql import parse_statements [as x]``, or
    ``.parse_statements`` on a name bound to that module."""
    if any(
        source == _CORE_SQL and original == "parse_statements"
        for source, original in _from_imports(tree).values()
    ):
        return True
    modules = _module_aliases(tree)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and node.attr == "parse_statements"):
            continue
        value = node.value
        path = ast.unparse(value)
        head, _, rest = path.partition(".")
        if head in modules and ".".join(filter(None, (modules[head], rest))) == (
            _CORE_SQL
        ):
            return True
    return False


def _parsing_problems(module: str, tree: ast.Module) -> list[str]:
    """Where a module parses, tokenizes or checks the floor other than through
    parse_statements; empty when it doesn't. Names are resolved through
    ``from ... import ... as`` aliases, and a reference counts as a use
    (``_p = sqlglot.parse_one`` is flagged where it is bound, not only where
    it is called)."""
    problems = []
    if not _uses_parse_statements(tree):
        problems.append(f"{module} imports sqlglot, not parse_statements")
    imports = _from_imports(tree)
    modules = _module_aliases(tree)

    def flag(node: ast.AST, name: str) -> None:
        line = f"{module}:{getattr(node, 'lineno', '?')}"
        if name in _CORE_ONLY:
            problems.append(f"{line} uses {name}: parse_statements does that")
        else:
            problems.append(f"{line} references {name}: parse with parse_statements")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_sqlglot(alias.name) and (
                    _PARSING_NAMES & set(alias.name.split("."))
                    or _parsing_module(alias.name)
                ):
                    flag(node, alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if _is_sqlglot(node.module) and (
                    alias.name == "*"
                    or alias.name in _PARSING_NAMES
                    or alias.name in _BUILDER_NAMES
                    or _PARSING_NAMES & set(node.module.split("."))
                    or _parsing_module(f"{node.module}.{alias.name}")
                ):
                    flag(node, f"{node.module}.{alias.name}")
                if node.module == _CORE_SQL and alias.name in _CORE_ONLY:
                    flag(node, alias.name)
        elif isinstance(node, ast.Attribute):
            if node.attr in _PARSING_NAMES or node.attr in _CORE_ONLY:
                flag(node, node.attr)
            elif (path := _sqlglot_path(node, modules)) is not None and (
                node.attr in _BUILDER_NAMES or _parsing_module(path)
            ):
                flag(node, path)
        elif isinstance(node, ast.Name):
            original = _resolved(node.id, imports)
            if original in _PARSING_NAMES or original in _CORE_ONLY:
                flag(node, original)
        elif isinstance(node, ast.Call):
            # getattr(sqlglot, "parse_one") and the like
            name = _constant_arg(node, 1)
            if _called_name(node) in _ATTRIBUTE_CALLS and (
                name in _PARSING_NAMES
                or name in _CORE_ONLY
                or (
                    (name in _BUILDER_NAMES or name in _PARSING_MODULES)
                    and len(node.args) > 0
                    and _sqlglot_path(node.args[0], modules) is not None
                )
            ):
                flag(node, str(name))
        elif isinstance(node, ast.ClassDef):
            for base in node.bases:
                text = ast.unparse(base)
                last = _resolved(text, imports).rsplit(".", 1)[-1]
                if last.endswith(("Parser", "Tokenizer")):
                    problems.append(
                        f"{module}:{node.lineno} subclasses {text}: parse_statements' "
                        "parser is the quiet one"
                    )
    return problems


def _floor_problems(module: str, tree: ast.Module) -> list[str]:
    """Where a module keeps a sqlglot floor of its own (binds the name, sets
    core's, or hands parse_statements one); empty when it doesn't. A call of
    parse_statements under an alias (``ps(...)``) is read as one; any other
    reference to it (``_ps = parse_statements``, ``partial(parse_statements,
    ...)``) is flagged: a floor= handed through it would go unseen (#50
    review seq 14161 n1). So a guard calls parse_statements directly."""
    problems = []
    imports = _from_imports(tree)
    called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for node in ast.walk(tree):
        line = f"{module}:{getattr(node, 'lineno', '?')}"
        if isinstance(node, (ast.Name, ast.Attribute)) and id(node) not in called:
            name = node.id if isinstance(node, ast.Name) else node.attr
            if _resolved(name, imports) == "parse_statements":
                problems.append(
                    f"{line} passes parse_statements around ({ast.unparse(node)}): "
                    "call it, so no floor= reaches it unseen"
                )
        if (
            isinstance(node, ast.Name)
            and node.id == _FLOOR
            and isinstance(node.ctx, (ast.Store, ast.Del))
        ):
            problems.append(f"{line} sets its own {_FLOOR}: import core's")
        elif (
            isinstance(node, ast.Attribute)
            and node.attr == _FLOOR
            and isinstance(node.ctx, (ast.Store, ast.Del))
        ):
            problems.append(f"{line} sets {ast.unparse(node)}: core's is the floor")
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                core_floor = (
                    isinstance(node, ast.ImportFrom)
                    and node.module == _CORE_SQL
                    and alias.name == _FLOOR
                )
                if (alias.asname or alias.name) == _FLOOR and not core_floor:
                    problems.append(f"{line} binds {_FLOOR} to {alias.name}")
        elif isinstance(node, ast.Call):
            if _resolved(_called_name(node), imports) == "parse_statements":
                for keyword in node.keywords:
                    if keyword.arg is None or keyword.arg == "floor":
                        problems.append(
                            f"{line} passes parse_statements a floor "
                            f"({ast.unparse(keyword)}): core's is the floor"
                        )
            elif _called_name(node) == "setattr" and _constant_arg(node, 1) == _FLOOR:
                problems.append(
                    f"{line} sets {_FLOOR} with setattr: core's is the floor"
                )
    return problems


def _sqlglot_modules() -> dict[str, ast.Module]:
    found = {}
    for path in sorted(ROOT.glob("connectors/*/src/**/*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if _imports_sqlglot(tree):
            found[str(path.relative_to(ROOT))] = tree
    return found


_GUARDS = (
    "connectors/aws/src/aisquare_pipe_aws/redshift/sqlguard.py",
    "connectors/bigquery/src/aisquare_pipe_bigquery/sqlguard.py",
    "connectors/databricks/src/aisquare_pipe_databricks/sqlguard.py",
    "connectors/snowflake/src/aisquare_pipe_snowflake/sqlguard.py",
)


def test_the_floor_is_the_one_dep_floor_reads_from_the_pyproject():
    floor = _dep_floor().floor(ROOT, "sqlglot")
    assert SQLGLOT_FLOOR == sqlglot_version(floor.version), (
        f"core.sql.SQLGLOT_FLOOR is {SQLGLOT_FLOOR}, the pyproject's extras say "
        f"{floor.specifier}: raise both together"
    )


def test_the_known_guards_are_found():
    found = _sqlglot_modules()
    for guard in _GUARDS:
        assert guard in found


@pytest.mark.parametrize("module", sorted(_sqlglot_modules()))
def test_every_module_importing_sqlglot_parses_through_parse_statements(module):
    assert _parsing_problems(module, _sqlglot_modules()[module]) == []


@pytest.mark.parametrize("module", sorted(_sqlglot_modules()))
def test_no_guard_keeps_its_own_floor(module):
    assert _floor_problems(module, _sqlglot_modules()[module]) == []


# -- the tripwire catches what it is for (mutations, in memory) ---------------------------

#: Each is appended to a real guard's source, and the tripwire must flag it.
#: The first four passed it at 910e13f (#46 re-review seq 14138 nit c); the
#: dialect's parser() is how the Databricks guard parsed at 77eb6e4.
PARSING_MUTATIONS = {
    "maybe_parse": "def _m(sql):\n    return exp.maybe_parse(sql)\n",
    "an aliased import": (
        "from sqlglot import parse_one as po\n\ndef _m(sql):\n    return po(sql)\n"
    ),
    "a reference, called later": (
        "import sqlglot\n_p = sqlglot.parse_one\n\ndef _m(sql):\n    return _p(sql)\n"
    ),
    "a subclass of an aliased Parser": (
        "from sqlglot.parser import Parser as P\n\nclass Q(P):\n    pass\n"
    ),
    "a Parser bound to a name": "P = type(_DIALECT).Parser\n\nclass Q(P):\n    pass\n",
    "the dialect's parser()": (
        "def _m(sql):\n    return _DIALECT.parser().parse(_DIALECT.tokenize(sql), sql)\n"
    ),
    "the parser class": "_C = type(_DIALECT).parser_class\n",
    "a tokenizer": "def _m(sql):\n    return _DIALECT.tokenizer.tokenize(sql)\n",
    "getattr": (
        "import sqlglot\n\ndef _m(sql):\n    return getattr(sqlglot, 'parse_one')(sql)\n"
    ),
    "a star import": "from sqlglot import *\n",
    "the parser module": "import sqlglot.parser\n",
    "transpile": "import sqlglot\n_t = sqlglot.transpile\n",
    "core's floor check, aliased": (
        "from aisquare.pipe.core.sql import sqlglot_at_least as ok\n"
        "_OK = ok('27.0', (26, 25))\n"
    ),
    "core's quiet parser": (
        "from aisquare.pipe.core.sql import quiet_parser_class\n"
        "_P = quiet_parser_class(_DIALECT)\n"
    ),
    # #50 review seq 14161 n2: string builders and the optimizer, which parse
    # (and log) outside parse_statements. Each passed at a554b7b.
    "exp.condition": (
        "from sqlglot import exp\n\ndef _m(sql):\n    return exp.condition(sql)\n"
    ),
    "exp.select": (
        "from sqlglot import exp\n\ndef _m(sql):\n    return exp.select(sql)\n"
    ),
    "a builder, imported under another name": (
        "from sqlglot.expressions import to_table as tt\n\n"
        "def _m(sql):\n    return tt(sql)\n"
    ),
    "DataType.build": (
        "from sqlglot import exp\n\ndef _m(t):\n    return exp.DataType.build(t)\n"
    ),
    "a builder through getattr": (
        "from sqlglot import exp\n\ndef _m(sql):\n"
        "    return getattr(exp, 'condition')(sql)\n"
    ),
    "the optimizer": (
        "import sqlglot\nimport sqlglot.optimizer\n\n"
        "def _m(sql):\n    return sqlglot.optimizer.optimize(sql)\n"
    ),
    "the optimizer, imported from": (
        "from sqlglot.optimizer import optimize\n\n"
        "def _m(sql):\n    return optimize(sql)\n"
    ),
    "the executor": "import sqlglot.executor as ex\n",
}
FLOOR_MUTATIONS = {
    "an assignment": "SQLGLOT_FLOOR = (25, 0)\n",
    "an annotated one": "SQLGLOT_FLOOR: tuple[int, ...] = (25, 0)\n",
    "a tuple target": "(SQLGLOT_FLOOR, _X) = ((25, 0), 1)\n",
    "a floor passed to parse_statements": (
        "def _m(sql):\n    return parse_statements(sql, _DIALECT, floor=(25, 0))\n"
    ),
    "a floor passed by **": (
        "def _m(sql):\n"
        "    return parse_statements(sql, _DIALECT, **{'floor': (25, 0)})\n"
    ),
    "core's, overwritten": (
        "from aisquare.pipe.core import sql as _core\n_core.SQLGLOT_FLOOR = (25, 0)\n"
    ),
    "setattr on core": (
        "from aisquare.pipe.core import sql as _core\n"
        "setattr(_core, 'SQLGLOT_FLOOR', (25, 0))\n"
    ),
    "another name imported as the floor": (
        "from aisquare.pipe.core.sql import SQLGLOT_LOGGER as SQLGLOT_FLOOR\n"
    ),
    "a floor from elsewhere": "from aisquare_pipe_x.floors import SQLGLOT_FLOOR\n",
    # #50 review seq 14161 n1: parse_statements under another name, or
    # handed on before it is called. Each passed at a554b7b.
    "a floor passed to an aliased import": (
        "from aisquare.pipe.core.sql import parse_statements as ps\n\n"
        "def _m(sql):\n    return ps(sql, _DIALECT, floor=(25, 0))\n"
    ),
    "a floor passed to a local alias": (
        "_ps = parse_statements\n\n"
        "def _m(sql):\n    return _ps(sql, _DIALECT, floor=(25, 0))\n"
    ),
    "a floor bound by functools.partial": (
        "import functools\n_ps = functools.partial(parse_statements, floor=(25, 0))\n"
    ),
}


def _mutated(guard: str, extra: str) -> ast.Module:
    source = (ROOT / guard).read_text(encoding="utf-8")
    return ast.parse(source + "\n\n" + extra)


@pytest.mark.parametrize("guard", _GUARDS)
@pytest.mark.parametrize("mutation", sorted(PARSING_MUTATIONS))
def test_the_tripwire_flags_parsing_outside_parse_statements(guard, mutation):
    assert _parsing_problems(guard, _mutated(guard, PARSING_MUTATIONS[mutation]))


@pytest.mark.parametrize("guard", _GUARDS)
@pytest.mark.parametrize("mutation", sorted(FLOOR_MUTATIONS))
def test_the_tripwire_flags_a_floor_of_its_own(guard, mutation):
    assert _floor_problems(guard, _mutated(guard, FLOOR_MUTATIONS[mutation]))


def test_the_tripwire_flags_a_module_without_parse_statements():
    assert _parsing_problems("m.py", ast.parse("from sqlglot import exp\n")) == [
        "m.py imports sqlglot, not parse_statements"
    ]
    for source in (
        "from aisquare.pipe.core.sql import parse_statements as ps\n",
        "from aisquare.pipe.core import sql as c\n_ps = c.parse_statements\n",
        "import aisquare.pipe.core.sql as c\n_ps = c.parse_statements\n",
        "import aisquare.pipe.core.sql\n_ps = aisquare.pipe.core.sql.parse_statements\n",
    ):
        assert _parsing_problems("m.py", ast.parse(source)) == [], source
    # parse_statements from anywhere else is not core's.
    assert _parsing_problems(
        "m.py", ast.parse("from aisquare_pipe_x.sql import parse_statements\n")
    )


def test_builder_names_count_only_where_they_are_sqlglot_s():
    """``case``, ``select``, ``union`` as a guard's own names, a method of
    something else, or sqlglot's classes (``exp.Select``) are not parsing."""
    source = (
        "from aisquare.pipe.core.sql import parse_statements\n"
        "from sqlglot import exp\n"
        "def _m(sql, rows, case=None):\n"
        "    select = union = parse_statements(sql, _DIALECT).statements\n"
        "    rows.update({}); rows.select(1)\n"
        "    return [s for s in select if isinstance(s, (exp.Select, exp.Union))], case\n"
    )
    assert _parsing_problems("m.py", ast.parse(source)) == []
    assert _floor_problems("m.py", ast.parse(source)) == []


def test_a_dynamic_import_of_sqlglot_is_scanned_too():
    assert _imports_sqlglot(
        ast.parse("import importlib\n_s = importlib.import_module('sqlglot')\n")
    )
    assert _imports_sqlglot(ast.parse("_s = __import__('sqlglot.parser')\n"))
    assert not _imports_sqlglot(ast.parse("import json\n"))


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


class _Refused(Exception):
    pass


def test_check_tokens_sees_the_tokens_before_they_are_parsed(bigquery):
    seen = []
    parsed = parse_statements("SELECT 1", bigquery, check_tokens=seen.append)
    assert [t.text for t in seen[0]] == [t.text for t in parsed.tokens]
    assert [s.key for s in parsed.statements] == ["select"]


def test_what_check_tokens_raises_propagates_and_nothing_is_parsed(
    bigquery, monkeypatch
):
    def refuse(tokens):
        raise _Refused("its own reason")

    def parse(self, *args, **kwargs):
        raise AssertionError("parsed after check_tokens refused")

    monkeypatch.setattr(core_sql.quiet_parser_class(bigquery), "parse", parse)
    with pytest.raises(_Refused, match="its own reason"):
        parse_statements("SELECT 1", bigquery, check_tokens=refuse)


def test_check_tokens_keeps_its_reason_where_the_text_would_not_parse(bigquery):
    def refuse(tokens):
        raise _Refused("its own reason")

    with pytest.raises(Unparsable):
        parse_statements("SELECT FROM WHERE", bigquery)
    with pytest.raises(_Refused):
        parse_statements("SELECT FROM WHERE", bigquery, check_tokens=refuse)


def test_check_tokens_is_not_called_when_tokenizing_fails(bigquery):
    called = []
    with pytest.raises(Unparsable):
        parse_statements("SELECT 'never closed", bigquery, check_tokens=called.append)
    assert called == []


def test_check_tokens_gets_a_copy_so_what_is_parsed_stays_sqlglot_s(bigquery):
    parsed = parse_statements(
        "SELECT 1; DELETE FROM t", bigquery, check_tokens=list.clear
    )
    assert [s.key for s in parsed.statements] == ["select", "delete"]
