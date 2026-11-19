"""scripts/check_toolkits_extra.py: the [toolkits] extra pulls in every toolkit connector, nothing else."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from aisquare.pipe import TOOLKIT_NAME_RULE

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_toolkits_extra.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("check_toolkits_extra", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


ctx = _load_script()


def _repo(
    tmp_path: Path,
    toolkits_extra: list[str] | None,
    *,
    toolkit_names: dict[str, str] | None = None,
    extras: tuple[str, ...] = ("alpha", "beta", "plain"),
    dirs: tuple[str, ...] = ("alpha", "beta", "plain"),
) -> Path:
    """A pipe checkout: connectors alpha and beta register toolkits (named
    ``toolkit_names[dir]``, default the dir), plain registers a connector only.
    ``toolkits_extra`` is the [toolkits] list (None: no such extra)."""
    toolkit_names = {"alpha": "alpha", "beta": "beta", **(toolkit_names or {})}
    for d in dirs:
        (tmp_path / "connectors" / d / "src" / f"aisquare_pipe_{d}").mkdir(parents=True)
    lines = [
        "[project]",
        'name = "aisquare-pipe"',
        'version = "0.5.0"',
        "",
        "[project.optional-dependencies]",
    ]
    lines += [f"{e} = []" for e in extras]
    if toolkits_extra is not None:
        lines.append(
            f"toolkits = [{', '.join(repr(e).replace(chr(39), chr(34)) for e in toolkits_extra)}]"
        )
    lines += ["", '[project.entry-points."aisquare_pipe.connectors"]']
    lines += [f'"{d}-source" = "aisquare_pipe_{d}.connector:Source"' for d in dirs]
    lines += ['mock-source = "aisquare.pipe.testing.mock_connectors:MockSource"']
    lines += ["", '[project.entry-points."aisquare_pipe.toolkits"]']
    lines += [
        f'"{name}" = "aisquare_pipe_{d}.toolkit:Toolkit"'
        for d, name in toolkit_names.items()
        if d in dirs
    ]
    lines += ['mock = "aisquare.pipe.testing.mock_connectors:MockToolkit"']
    (tmp_path / "pyproject.toml").write_text("\n".join(lines) + "\n")
    return tmp_path


GOOD = ["aisquare-pipe[alpha,beta]"]


def test_the_extra_on_this_branch_pulls_in_every_toolkit_connector(capsys):
    assert ctx.check(REPO_ROOT) == []
    assert ctx.main(["--root", str(REPO_ROOT)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("check_toolkits_extra: ok")
    # The six toolkit connectors of the native-connectors release, at least.
    for name in (
        "github",
        "gmail",
        "googlecalendar",
        "googledrive",
        "onedrive",
        "slack",
    ):
        assert name in out


def test_the_toolkit_connectors_on_this_branch():
    found = {c.dir_name for c in ctx.toolkit_connectors(REPO_ROOT)}
    assert {"github", "slack"} <= found
    assert "composio" not in found  # a connector, no toolkit


def test_a_well_formed_extra_passes(tmp_path):
    root = _repo(tmp_path, GOOD)
    assert ctx.check(root) == []
    assert ctx.main(["--root", str(root)]) == 0


def test_names_compare_as_pip_compares_them(tmp_path):
    root = _repo(tmp_path, ["aisquare_pipe[Alpha]", "Aisquare.Pipe[beta]>=0.5"])
    assert ctx.check(root) == []


def test_the_framework_mock_is_not_a_toolkit_connector(tmp_path):
    root = _repo(tmp_path, GOOD)
    assert [c.dir_name for c in ctx.toolkit_connectors(root)] == ["alpha", "beta"]


def test_a_toolkit_connector_missing_from_the_extra_fails(tmp_path, capsys):
    root = _repo(tmp_path, ["aisquare-pipe[alpha]"])
    [problem] = ctx.check(root)
    assert "connectors/beta registers toolkit(s) beta" in problem
    assert "[beta] is not in the toolkits extra" in problem
    assert ctx.main(["--root", str(root)]) == 1
    assert "1 problem(s)" in capsys.readouterr().err


def test_a_new_toolkit_connector_fails_until_it_is_added(tmp_path):
    root = _repo(
        tmp_path,
        GOOD,
        dirs=("alpha", "beta", "plain", "gamma"),
        extras=("alpha", "beta", "plain", "gamma"),
        toolkit_names={"gamma": "gamma"},
    )
    [problem] = ctx.check(root)
    assert "connectors/gamma registers toolkit(s) gamma" in problem


def test_a_toolkit_connector_needs_its_own_extra(tmp_path):
    root = _repo(tmp_path, ["aisquare-pipe[alpha]"], extras=("alpha", "plain"))
    problems = ctx.check(root)
    assert any("connectors/beta has no [beta] extra" in p for p in problems), problems


@pytest.mark.parametrize(
    "entry, says",
    [
        ("aisquare-pipe[alpha,beta,plain]", "[plain] is not a toolkit connector"),
        ("aisquare-pipe[alpha,beta,nowhere]", "aisquare-pipe has no [nowhere] extra"),
    ],
)
def test_an_entry_without_a_toolkit_fails(tmp_path, entry, says):
    root = _repo(tmp_path, [entry])
    [problem] = ctx.check(root)
    assert repr(entry) in problem and says in problem


@pytest.mark.parametrize(
    "entry",
    [
        "aisquare-pipe-alpha>=0.3.0",
        "requests>=2.31",
        "aisquare-pipe[alpha] @ file:///x",
    ],
)
def test_entries_other_than_a_self_reference_fail(tmp_path, entry):
    root = _repo(tmp_path, [*GOOD, entry])
    [problem] = ctx.check(root)
    assert "write the toolkits as aisquare-pipe[<dir>,...]" in problem


def test_a_duplicate_entry_fails(tmp_path):
    root = _repo(tmp_path, [*GOOD, "aisquare-pipe[Alpha]"])
    [problem] = ctx.check(root)
    assert "listed twice" in problem


def test_no_extra_at_all_fails(tmp_path):
    root = _repo(tmp_path, None)
    [problem] = ctx.check(root)
    assert "no [project.optional-dependencies] toolkits extra" in problem


def test_dirs_lists_the_toolkit_connectors(tmp_path, capsys):
    root = _repo(tmp_path, GOOD).resolve()
    assert ctx.main(["--root", str(root), "--dirs"]) == 0
    dirs = capsys.readouterr().out.split()
    assert dirs == [
        str(root / "connectors" / "alpha"),
        str(root / "connectors" / "beta"),
    ]


# =============================================================================
# toolkit names (TOOLKIT_NAME_RULE: what a host can route)
# =============================================================================


def test_the_script_holds_toolkits_to_pipes_name_rule():
    assert ctx.TOOLKIT_NAME_RULE == TOOLKIT_NAME_RULE


@pytest.mark.parametrize("name", ["led-ger", "Ledger", "led.ger", "l" * 65])
def test_a_toolkit_name_hosts_cannot_route_fails(tmp_path, name):
    root = _repo(tmp_path, GOOD, toolkit_names={"beta": name})
    [problem] = ctx.check(root)
    assert f"connectors/beta: toolkit entry point {name!r} must match" in problem
    assert TOOLKIT_NAME_RULE in problem and "no hyphens" in problem
    assert ctx.main(["--root", str(root)]) == 1


@pytest.mark.parametrize("name", ["ledger", "led_ger", "l" * 64])
def test_underscored_and_plain_toolkit_names_pass(tmp_path, name):
    root = _repo(tmp_path, GOOD, toolkit_names={"beta": name})
    assert ctx.check(root) == []


def test_connector_entry_points_may_keep_their_hyphens(tmp_path):
    """Only the toolkits group is bound: every fixture connector registers a
    hyphenated ``<dir>-source`` connector, and the repo passes."""
    root = _repo(tmp_path, GOOD)
    assert '"alpha-source" = "' in (root / "pyproject.toml").read_text()
    assert ctx.check(root) == []
