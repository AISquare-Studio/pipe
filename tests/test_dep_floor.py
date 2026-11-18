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


def test_no_matching_connector_directory_fails_the_floor_step(tmp_path, capsys):
    """The CI floor step would pass having tested nothing when --connectors
    prints nothing (review #37 n1): that exits 1, and says why."""
    root = _repo(tmp_path, {"alpha": ["sqlglot>=26.25,<28"]})
    (root / "connectors" / "alpha").rmdir()
    (root / "connectors" / "beta").mkdir()
    assert df.main(["sqlglot", "--root", str(root)]) == 0
    capsys.readouterr()
    assert df.main(["sqlglot", "--root", str(root), "--connectors"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no connectors/<dir>" in captured.err and "alpha" in captured.err


def test_an_extra_naming_it_with_no_connector_directory_fails(tmp_path, capsys):
    """Review seq 14088 n3: one suite matched, so the step passed, and the
    extra with no connectors/<extra> directory (beta) was skipped silently:
    its guard never ran at the floor. Now that exits 1, naming it."""
    root = _repo(
        tmp_path, {"alpha": ["sqlglot>=26.25,<28"], "beta": ["sqlglot>=26.25,<28"]}
    )
    (root / "connectors" / "beta").rmdir()
    assert df.main(["sqlglot", "--root", str(root), "--connectors"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "[beta]" in captured.err and "connectors/beta" in captured.err


def test_an_aggregate_extra_needs_no_connector_directory(tmp_path, capsys):
    """[full] names the project itself (aisquare-pipe[...]): an aggregate,
    with no directory of its own, even when it names sqlglot too."""
    root = _repo(
        tmp_path,
        {
            "alpha": ["sqlglot>=26.25,<28"],
            "full": ["aisquare-pipe[alpha]", "sqlglot>=26.25,<28"],
        },
    )
    (root / "connectors" / "full").rmdir()
    assert df.main(["sqlglot", "--root", str(root), "--connectors"]) == 0
    assert capsys.readouterr().out.splitlines() == [str(root / "connectors" / "alpha")]


def test_the_floor_step_reads_the_list_before_it_loops():
    """validate.yml reads --connectors into a variable (set -e stops on its
    failure there, not in a for list) and fails when no suite ran."""
    workflow = (REPO_ROOT / ".github" / "workflows" / "validate.yml").read_text()
    step = workflow.split("name: SQL guards at the sqlglot floor", 1)[1]
    assert 'dirs="$(python scripts/dep_floor.py sqlglot --connectors)"' in step
    assert "for dir in $dirs; do" in step
    assert 'test "$ran" -gt 0' in step
    assert "for dir in $(" not in step
