"""The root pyproject packages every connector into the one aisquare-pipe
distribution (D8), in step with connectors/.

These read files only. The built wheel itself is checked against connectors/
by ``scripts/release_packages.py check-dist`` in publish.yml's build job (on
every PR): its RECORD must hold every module under src/ and every
connectors/<dir>/src/.

Adding a connector? Put it at connectors/<dir>/src/aisquare_pipe_<dir>/, then
in the root pyproject.toml: its src dir in [tool.setuptools.packages.find]
where, its entry points, an extra named <dir> (its third-party deps, [] if
none), and <dir> in [full] (and in [toolkits] if it registers a toolkit).
"""

from __future__ import annotations

import importlib.util
import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ROOT = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
PROJECT = ROOT["project"]
GROUPS = (
    "aisquare_pipe.connectors",
    "aisquare_pipe.toolkits",
    "aisquare_pipe.converters",
)
CONNECTORS = sorted(
    d.name
    for d in (REPO_ROOT / "connectors").iterdir()
    if d.is_dir() and not d.name.startswith((".", "_"))
)


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _module(d: str) -> str:
    return f"aisquare_pipe_{d.replace('-', '_')}"


EXTRAS = {_canonical(k): v for k, v in PROJECT["optional-dependencies"].items()}
ENTRY_POINTS = {
    (group, name): value
    for group in GROUPS
    for name, value in PROJECT["entry-points"].get(group, {}).items()
}


def _bundle(extra: str) -> set[str]:
    """Every extra ``extra`` pulls in through aisquare-pipe[...] references."""
    seen, todo = set(), [_canonical(extra)]
    while todo:
        name = todo.pop()
        if name not in seen:
            seen.add(name)
            for req in EXTRAS.get(name, []):
                m = re.match(r"^\s*aisquare-pipe\s*\[([^\]]*)\]", req)
                todo += [_canonical(e) for e in m.group(1).split(",")] if m else []
    return seen


def test_there_are_connectors():
    assert {"github", "slack", "composio"} <= set(CONNECTORS)


def test_the_wheel_packages_src_and_every_connector_src_dir():
    where = ROOT["tool"]["setuptools"]["packages"]["find"]["where"]
    assert where == ["src", *(f"connectors/{d}/src" for d in CONNECTORS)]


@pytest.mark.parametrize("d", CONNECTORS)
def test_each_connector_is_one_module_with_an_extra_in_full(d):
    assert (REPO_ROOT / "connectors" / d / "src" / _module(d) / "__init__.py").is_file()
    assert _canonical(d) in EXTRAS, f"no [{d}] extra in the root pyproject"
    assert _canonical(d) in _bundle("full"), f"[full] doesn't pull in [{d}]"


@pytest.mark.parametrize("d", CONNECTORS)
def test_each_connector_registers_an_entry_point_in_the_root(d):
    own = [
        k
        for k, v in ENTRY_POINTS.items()
        if v.split(":")[0].split(".")[0] == _module(d)
    ]
    assert own, f"no entry point in the root pyproject points into {_module(d)}"


@pytest.mark.parametrize("d", CONNECTORS)
def test_a_connector_pyproject_holds_tool_settings_only(d):
    data = tomllib.loads((REPO_ROOT / "connectors" / d / "pyproject.toml").read_text())
    assert not {"project", "build-system"} & set(data), (
        "packaging lives in the root pyproject"
    )


@pytest.mark.parametrize(("group", "name"), sorted(ENTRY_POINTS))
def test_every_entry_point_names_a_module_file_in_the_checkout(group, name):
    module, _, attr = ENTRY_POINTS[(group, name)].partition(":")
    assert attr, f"{name} has no :attribute"
    top, *rest = module.split(".")
    if top == "aisquare":
        base = REPO_ROOT / "src"
        parts = [top, *rest]
    else:
        d = next((c for c in CONNECTORS if _module(c) == top), None)
        assert d is not None, (
            f"{name} points into {top}, which no connectors/<dir> provides"
        )
        base = REPO_ROOT / "connectors" / d / "src"
        parts = [top, *rest]
    path = base.joinpath(*parts)
    assert path.with_suffix(".py").is_file() or (path / "__init__.py").is_file(), (
        f"{module} not found"
    )


def test_toolkits_is_a_bundle_of_toolkit_connectors():
    toolkit_modules = {
        v.split(".")[0]
        for (g, _), v in ENTRY_POINTS.items()
        if g == "aisquare_pipe.toolkits"
    }
    toolkit_dirs = {_canonical(d) for d in CONNECTORS if _module(d) in toolkit_modules}
    assert _bundle("toolkits") - {"toolkits"} == toolkit_dirs


def test_core_dependencies_never_name_another_aisquare_pipe_distribution():
    every = [*PROJECT["dependencies"], *(r for reqs in EXTRAS.values() for r in reqs)]
    assert not [
        r for r in every if re.match(r"^\s*aisquare[-_.]pipe[-_.]", r, re.IGNORECASE)
    ]


def test_the_license_is_an_spdx_expression():
    """PEP 639: a license table and License classifiers are deprecated
    (setuptools warns, and drops them after 2027-02-18)."""
    assert PROJECT["license"] == "Apache-2.0"
    assert not [c for c in PROJECT["classifiers"] if c.startswith("License ::")]


@pytest.mark.skipif(
    importlib.util.find_spec("aisquare_pipe_github") is None,
    reason="needs the editable install",
)
def test_the_installed_distribution_carries_the_connector_modules():
    from importlib.metadata import distribution, entry_points

    names = {
        (ep.group, ep.name)
        for group in GROUPS
        for ep in entry_points(group=group)
        if ep.dist
    }
    dist = {
        (ep.group, ep.name)
        for ep in distribution("aisquare-pipe").entry_points
        if ep.group in GROUPS
    }
    assert set(ENTRY_POINTS) <= dist <= names
