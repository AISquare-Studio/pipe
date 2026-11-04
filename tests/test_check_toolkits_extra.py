"""scripts/check_toolkits_extra.py: the [toolkits] extra lists every toolkit package, nothing else."""

from __future__ import annotations

import importlib.util
import sys
import textwrap
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


def _connector(
    root: Path,
    dirname: str,
    *,
    version: str = "0.1.0",
    toolkit: bool,
    toolkit_name: str | None = None,
) -> None:
    pkg = root / "connectors" / dirname
    pkg.mkdir(parents=True)
    module = f"aisquare_pipe_{dirname}"
    toolkit_ep = (
        f'[project.entry-points."aisquare_pipe.toolkits"]\n'
        f'"{toolkit_name or dirname}" = "{module}.toolkit:Toolkit"\n'
        if toolkit
        else ""
    )
    (pkg / "pyproject.toml").write_text(
        textwrap.dedent(
            f"""\
            [project]
            name = "aisquare-pipe-{dirname}"
            version = "{version}"

            [project.entry-points."aisquare_pipe.connectors"]
            {dirname}-source = "{module}.connector:Source"

            """
        )
        + toolkit_ep
    )


def _repo(tmp_path: Path, toolkits_extra: list[str] | None) -> Path:
    """A pipe checkout: alpha and beta register toolkits, plain does not."""
    extras = ""
    if toolkits_extra is not None:
        entries = ", ".join(f'"{e}"' for e in toolkits_extra)
        extras = f"[project.optional-dependencies]\ntoolkits = [{entries}]\n"
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "aisquare-pipe"\nversion = "0.4.0"\n\n{extras}'
    )
    _connector(tmp_path, "alpha", version="0.3.0", toolkit=True)
    _connector(tmp_path, "beta", toolkit=True)
    _connector(tmp_path, "plain", toolkit=False)
    return tmp_path


GOOD = ["aisquare-pipe-alpha>=0.3.0", "aisquare-pipe-beta>=0.1.0"]


def test_the_extra_on_this_branch_lists_every_toolkit_package(capsys):
    assert ctx.check(REPO_ROOT) == []
    assert ctx.main(["--root", str(REPO_ROOT)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("check_toolkits_extra: ok")
    # The six toolkit packages of the native-connectors release, at least.
    for name in (
        "github",
        "gmail",
        "googlecalendar",
        "googledrive",
        "onedrive",
        "slack",
    ):
        assert f"aisquare-pipe-{name}" in out


def test_the_extra_on_this_branch_names_exactly_the_toolkit_packages():
    found = {ctx.canonical(p.name) for p in ctx.toolkit_packages(REPO_ROOT)}
    assert {"aisquare-pipe-github", "aisquare-pipe-slack"} <= found
    assert "aisquare-pipe-composio" not in found  # a connector, no toolkit


def test_a_well_formed_extra_passes(tmp_path):
    root = _repo(tmp_path, GOOD)
    assert ctx.check(root) == []
    assert ctx.main(["--root", str(root)]) == 0


def test_names_compare_as_pip_compares_them(tmp_path):
    root = _repo(tmp_path, ["aisquare_pipe.Alpha>=0.3", "aisquare-pipe-beta[x]>=0.1.0"])
    assert ctx.check(root) == []


def test_a_toolkit_package_missing_from_the_extra_fails(tmp_path, capsys):
    root = _repo(tmp_path, ["aisquare-pipe-alpha>=0.3.0"])
    [problem] = ctx.check(root)
    assert "connectors/beta registers toolkit(s) beta" in problem
    assert "'aisquare-pipe-beta>=0.1.0'" in problem
    assert ctx.main(["--root", str(root)]) == 1
    assert "1 problem(s)" in capsys.readouterr().err


def test_a_new_toolkit_package_fails_until_it_is_added(tmp_path):
    root = _repo(tmp_path, GOOD)
    _connector(root, "gamma", version="0.2.0", toolkit=True)
    [problem] = ctx.check(root)
    assert "aisquare-pipe-gamma is not in the toolkits extra" in problem


@pytest.mark.parametrize(
    "entry, says",
    [
        ("aisquare-pipe-plain>=0.1.0", "no package under connectors/ registers"),
        ("aisquare-pipe-nowhere>=0.1.0", "no package under connectors/ registers"),
    ],
)
def test_an_entry_without_a_toolkit_fails(tmp_path, entry, says):
    root = _repo(tmp_path, [*GOOD, entry])
    [problem] = ctx.check(root)
    assert repr(entry) in problem and says in problem


@pytest.mark.parametrize(
    "entry, says",
    [
        ("aisquare-pipe-beta", "write it as 'aisquare-pipe-beta>=<floor>'"),
        ("aisquare-pipe-beta==0.1.0", "write it as"),
        ("aisquare-pipe-beta>=0.1.0,<1", "write it as"),
        ("aisquare-pipe-beta>=0.2.0", "floor 0.2.0 is above aisquare-pipe-beta 0.1.0"),
        ("aisquare-pipe-beta @ file:///x", "not a plain requirement"),
    ],
)
def test_an_entry_needs_a_floor_the_repo_satisfies(tmp_path, entry, says):
    root = _repo(tmp_path, ["aisquare-pipe-alpha>=0.3.0", entry])
    problems = ctx.check(root)
    assert any(says in p for p in problems), problems


def test_a_duplicate_entry_fails(tmp_path):
    root = _repo(tmp_path, [*GOOD, "aisquare_pipe_alpha>=0.1.0"])
    [problem] = ctx.check(root)
    assert "listed twice" in problem


def test_no_extra_at_all_fails(tmp_path):
    root = _repo(tmp_path, None)
    [problem] = ctx.check(root)
    assert "no [project.optional-dependencies] toolkits extra" in problem


def test_dirs_lists_the_toolkit_packages_for_the_dev_install(tmp_path, capsys):
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
    root = _repo(tmp_path, [*GOOD, "aisquare-pipe-ledger>=0.1.0"])
    _connector(root, "ledger", toolkit=True, toolkit_name=name)
    [problem] = ctx.check(root)
    assert f"connectors/ledger: toolkit entry point {name!r} must match" in problem
    assert TOOLKIT_NAME_RULE in problem and "no hyphens" in problem
    assert ctx.main(["--root", str(root)]) == 1


@pytest.mark.parametrize("name", ["ledger", "led_ger", "l" * 64])
def test_underscored_and_plain_toolkit_names_pass(tmp_path, name):
    root = _repo(tmp_path, [*GOOD, "aisquare-pipe-ledger>=0.1.0"])
    _connector(root, "ledger", toolkit=True, toolkit_name=name)
    assert ctx.check(root) == []


def test_connector_entry_points_may_keep_their_hyphens(tmp_path):
    """Only the toolkits group is bound: every fixture package registers a
    hyphenated ``<name>-source`` connector, and the repo passes."""
    root = _repo(tmp_path, GOOD)
    text = (root / "connectors" / "alpha" / "pyproject.toml").read_text()
    assert 'alpha-source = "' in text
    assert ctx.check(root) == []
