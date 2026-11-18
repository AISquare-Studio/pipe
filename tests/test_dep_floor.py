"""scripts/dep_floor.py: one floor per dependency across the extras, for CI to
run the suites that use it at that floor (validate.yml)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dep_floor.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("dep_floor", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


df = _load_script()


def _repo(tmp_path: Path, extras: dict[str, list[str]]) -> Path:
    for name in extras:
        (tmp_path / "connectors" / name).mkdir(parents=True)
    lines = [
        "[project]",
        'name = "aisquare-pipe"',
        "",
        "[project.optional-dependencies]",
    ]
    for name, requirements in extras.items():
        lines.append(f"{name} = {json.dumps(requirements)}")
    (tmp_path / "pyproject.toml").write_text("\n".join(lines) + "\n")
    return tmp_path


def test_the_sql_guards_floor_is_sqlglot_26_25_or_later():
    """sqlglot 25.3 to 26.24 read BigQuery's /* */ comments as nested, which
    GoogleSQL doesn't (the #37 bypass): the BigQuery guard's parser needs
    26.25 or later, and every extra naming sqlglot shares that floor."""
    found = df.floor(REPO_ROOT, "sqlglot")
    assert tuple(int(p) for p in found.version.split(".")[:2]) >= (26, 25)
    assert "bigquery" in found.extras
    dirs = [p.name for p in df.connectors(REPO_ROOT, found)]
    assert "bigquery" in dirs


def test_it_prints_the_floor_pin_and_the_connectors(tmp_path, capsys):
    root = _repo(
        tmp_path,
        {
            "alpha": ["sqlglot >= 26.25, <28", "requests"],
            "beta": ["sqlglot>=26.25,<28; python_version >= '3.11'"],
            "plain": [],
            "full": ["aisquare-pipe[alpha,beta,plain]"],
        },
    )
    assert df.main(["sqlglot", "--root", str(root)]) == 0
    assert capsys.readouterr().out == "sqlglot==26.25\n"
    assert df.main(["SQLGlot", "--root", str(root), "--connectors"]) == 0
    assert capsys.readouterr().out.split() == [
        str((root / "connectors" / d).resolve()) for d in ("alpha", "beta")
    ]


@pytest.mark.parametrize(
    ("extras", "problem"),
    [
        (
            {"alpha": ["sqlglot>=26.25,<28"], "beta": ["sqlglot>=25,<28"]},
            "different specifiers",
        ),
        ({"alpha": ["sqlglot<28"]}, "exactly one lower bound"),
        ({"alpha": ["requests"]}, "no extra names sqlglot"),
    ],
)
def test_it_refuses_extras_without_one_floor(tmp_path, capsys, extras, problem):
    root = _repo(tmp_path, extras)
    with pytest.raises(df.FloorError, match=problem):
        df.floor(root, "sqlglot")
    assert df.main(["sqlglot", "--root", str(root)]) == 1
    assert problem in capsys.readouterr().err
