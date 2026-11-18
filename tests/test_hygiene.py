"""Tests for the Layer B hygiene checks (testing/hygiene.py) against
tmp_path fake repo trees.

A fake repo mirrors the real layout (D8): one root pyproject (aisquare-pipe)
packages every connectors/<dir>/src, declares its entry points and its
[<dir>] extra; a connector's own pyproject holds tool settings only."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import unquote

import pytest

from aisquare.pipe.testing import hygiene
from aisquare.pipe.testing.hygiene import (
    DEV_INSTALL,
    INSTALL_BROKEN,
    INSTALL_MISSING,
    INSTALL_OK,
    ConnectorDir,
    DistInfo,
    _file_url_to_path,
    check_framework_install,
    check_install_state,
    classify_install_state,
    find_repo_root,
    list_connector_dirs,
    load_dist_info,
    run_hygiene_checks,
)
from aisquare.pipe.testing.validation import DiscoveredConnector, Severity

GOOD_CONNECTOR = '''\
"""Good connector."""


class GoodSource:
    name = "good-source"
    version = "0.1.0"
'''

TOOL_SETTINGS = """\
[tool.pytest.ini_options]
testpaths = ["tests"]
"""

_STATE = ".fixture-root.json"


def _write_root(root: Path) -> None:
    """Regenerate the root pyproject from the registered connectors."""
    state = json.loads((root / _STATE).read_text())
    where = ["src"] + [
        f"connectors/{d}/src" for d, c in sorted(state.items()) if c["packaged"]
    ]
    extras = {d: c["deps"] for d, c in sorted(state.items()) if c["extra"]}
    full = sorted(d for d, c in state.items() if c["extra"] and c["in_full"])
    groups: dict[str, dict[str, str]] = {}
    for c in state.values():
        for group, eps in c["entry_points"].items():
            groups.setdefault(group, {}).update(eps)
    text = '[project]\nname = "aisquare-pipe"\nversion = "0.5.0"\n\n[project.optional-dependencies]\n'
    text += "".join(
        f"{json.dumps(d)} = {json.dumps(deps)}\n" for d, deps in extras.items()
    )
    text += f'popular = []\nfull = ["aisquare-pipe[{",".join(["popular", *full])}]"]\n'
    for group, eps in sorted(groups.items()):
        text += f'\n[project.entry-points."{group}"]\n'
        text += "".join(f'{json.dumps(k)} = "{v}"\n' for k, v in sorted(eps.items()))
    text += f"\n[tool.setuptools.packages.find]\nwhere = {json.dumps(where)}\n"
    (root / "pyproject.toml").write_text(text, encoding="utf-8")


def build_repo(tmp_path: Path) -> Path:
    """A minimal fake repo with one fully-compliant connector `good`."""
    root = tmp_path / "repo"
    (root / "connectors").mkdir(parents=True)
    (root / _STATE).write_text("{}")
    _write_root(root)
    build_connector(root, "good")
    return root


def build_connector(
    root: Path,
    name: str,
    *,
    pyproject: str | None = TOOL_SETTINGS,
    connector_py: str | None = None,
    compliance: str
    | None = "from x import connector_compliance_suite\nconnector_compliance_suite(object)\n",
    entry_points: dict[str, dict[str, str]] | None = None,
    packaged: bool = True,
    extra: bool = True,
    in_full: bool = True,
    deps: list[str] | None = None,
) -> Path:
    """A connectors/<name> tree, registered in the root pyproject unless told
    otherwise (``packaged``/``extra``/``in_full``/``entry_points``)."""
    cdir = root / "connectors" / name
    safe = name.replace("-", "_")
    module = cdir / "src" / f"aisquare_pipe_{safe}"
    module.mkdir(parents=True)
    (cdir / "tests").mkdir()
    if pyproject is not None:
        (cdir / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    (cdir / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    (module / "__init__.py").write_text("", encoding="utf-8")
    (module / "connector.py").write_text(
        connector_py
        if connector_py is not None
        else GOOD_CONNECTOR.replace("good", name),
        encoding="utf-8",
    )
    (cdir / "tests" / "__init__.py").write_text("", encoding="utf-8")
    if compliance is not None:
        (cdir / "tests" / "test_compliance.py").write_text(compliance, encoding="utf-8")
    if entry_points is None:
        entry_points = {
            "aisquare_pipe.connectors": {
                f"{name}-source": f"aisquare_pipe_{safe}.connector:{name.title().replace('-', '')}Source"
            }
        }
    state = json.loads((root / _STATE).read_text())
    state[name] = {
        "packaged": packaged,
        "extra": extra,
        "in_full": in_full,
        "deps": deps or [],
        "entry_points": entry_points,
    }
    (root / _STATE).write_text(json.dumps(state))
    _write_root(root)
    return cdir


def get_cdir(root: Path, name: str) -> ConnectorDir:
    return next(c for c in list_connector_dirs(root) if c.dir_name == name)


def run(root: Path, name: str, dist: DistInfo | None = None) -> dict[str, object]:
    cdir = get_cdir(root, name)
    results = run_hygiene_checks(cdir, root, dist or DistInfo(found=False), {})
    return {r.id: r for r in results}


def _ok(results: dict, check_id: str) -> bool:
    return results[check_id].passed  # type: ignore[union-attr]


def _errors(results: dict) -> list:
    return [
        r for r in results.values() if not r.passed and r.severity is Severity.ERROR
    ]


class TestRepoDiscovery:
    def test_find_repo_root_walks_up(self, tmp_path):
        root = build_repo(tmp_path)
        nested = root / "connectors" / "good" / "src"
        assert find_repo_root(nested) == root

    def test_find_repo_root_none_outside(self, tmp_path):
        assert find_repo_root(tmp_path / "elsewhere") is None

    def test_list_connector_dirs(self, tmp_path):
        root = build_repo(tmp_path)
        build_connector(root, "second")
        names = [c.dir_name for c in list_connector_dirs(root)]
        assert names == ["good", "second"]

    def test_hyphenated_dir_module_name(self, tmp_path):
        root = build_repo(tmp_path)
        build_connector(root, "my-svc")
        cdir = get_cdir(root, "my-svc")
        assert cdir.module_name == "aisquare_pipe_my_svc"
        assert cdir.extra == "my-svc"
        assert cdir.requirement == cdir.dist_name == "aisquare-pipe[my-svc]"

    def test_entry_points_are_read_from_the_root_pyproject_by_module(self, tmp_path):
        root = build_repo(tmp_path)
        build_connector(
            root,
            "both",
            entry_points={
                "aisquare_pipe.connectors": {
                    "both-source": "aisquare_pipe_both.connector:BothSource"
                },
                "aisquare_pipe.toolkits": {
                    "both": "aisquare_pipe_both.toolkit:BothToolkit"
                },
            },
        )
        cdir = get_cdir(root, "both")
        assert cdir.entry_points("aisquare_pipe.connectors") == {
            "both-source": "aisquare_pipe_both.connector:BothSource"
        }
        assert cdir.registers_toolkit and cdir.registers_connector
        assert get_cdir(root, "good").declared_entry_points() == {"good-source"}


class TestGoodTreePasses:
    def test_no_error_failures(self, tmp_path):
        root = build_repo(tmp_path)
        results = run(root, "good", DistInfo(found=True, version="0.5.0"))
        assert _errors(results) == []

    def test_tier_listed_passes_through_full(self, tmp_path):
        root = build_repo(tmp_path)
        assert _ok(run(root, "good"), "hygiene.tier-listed")


class TestPackagingRules:
    def test_a_connector_pyproject_with_a_project_table_fails(self, tmp_path):
        root = build_repo(tmp_path)
        build_connector(
            root,
            "bad",
            pyproject='[build-system]\nrequires = ["setuptools"]\n\n[project]\nname = "aisquare-pipe-bad"\n'
            + TOOL_SETTINGS,
        )
        result = run(root, "bad")["hygiene.not-a-package"]
        assert not result.passed and "[build-system], [project]" in result.message

    def test_src_dir_not_packaged_fails(self, tmp_path):
        root = build_repo(tmp_path)
        build_connector(root, "bad", packaged=False)
        result = run(root, "bad")["hygiene.packaged"]
        assert not result.passed and '"connectors/bad/src"' in result.message

    def test_missing_extra_fails(self, tmp_path):
        root = build_repo(tmp_path)
        build_connector(root, "bad", extra=False)
        result = run(root, "bad")["hygiene.extra-declared"]
        assert (
            not result.passed and "aisquare-pipe[bad] does not exist" in result.message
        )

    def test_an_empty_extra_is_enough(self, tmp_path):
        root = build_repo(tmp_path)
        assert _ok(run(root, "good"), "hygiene.extra-declared")  # good = []

    def test_missing_entry_points(self, tmp_path):
        root = build_repo(tmp_path)
        build_connector(root, "bad", entry_points={})
        assert not _ok(run(root, "bad"), "hygiene.entry-points-registered")

    def test_another_connectors_entry_point_does_not_count(self, tmp_path):
        root = build_repo(tmp_path)
        build_connector(
            root,
            "bad",
            entry_points={
                "aisquare_pipe.connectors": {"stolen": "aisquare_pipe_good.connector:X"}
            },
        )
        assert not _ok(run(root, "bad"), "hygiene.entry-points-registered")

    def test_unparseable_pyproject(self, tmp_path):
        root = build_repo(tmp_path)
        build_connector(root, "bad", pyproject="not [ valid toml")
        assert not _ok(run(root, "bad"), "hygiene.pyproject-parses")

    def test_missing_pyproject(self, tmp_path):
        root = build_repo(tmp_path)
        build_connector(root, "bad", pyproject=None)
        result = run(root, "bad")["hygiene.pyproject-parses"]
        assert not result.passed and "pytest settings" in result.message


class TestScaffoldFiles:
    def test_missing_readme(self, tmp_path):
        root = build_repo(tmp_path)
        cdir = build_connector(root, "bad")
        (cdir / "README.md").unlink()
        result = run(root, "bad")["hygiene.scaffold-files"]
        assert not result.passed and "README.md" in result.message

    def test_missing_compliance_literal(self, tmp_path):
        root = build_repo(tmp_path)
        build_connector(root, "bad", compliance="def test_nothing():\n    pass\n")
        result = run(root, "bad")["hygiene.scaffold-files"]
        assert not result.passed and "test_compliance.py" in result.message

    def test_module_name_mismatch(self, tmp_path):
        root = build_repo(tmp_path)
        cdir = build_connector(root, "bad")
        (cdir / "src" / "aisquare_pipe_bad").rename(
            cdir / "src" / "aisquare_pipe_other"
        )
        assert not _ok(run(root, "bad"), "hygiene.module-name")


TOOLKIT_ONLY = {
    "aisquare_pipe.toolkits": {"kit": "aisquare_pipe_kit.toolkit:KitToolkit"}
}
BOTH = {
    "aisquare_pipe.connectors": {
        "both-source": "aisquare_pipe_both.connector:BothSource"
    },
    "aisquare_pipe.toolkits": {"both": "aisquare_pipe_both.toolkit:BothToolkit"},
}
TOOLKIT_COMPLIANCE = (
    "from x import toolkit_compliance_suite\ntoolkit_compliance_suite(object)\n"
)


class TestToolkitPackages:
    def test_toolkit_only_package_passes_without_connector_py(self, tmp_path):
        root = build_repo(tmp_path)
        cdir = build_connector(
            root, "kit", entry_points=TOOLKIT_ONLY, compliance=TOOLKIT_COMPLIANCE
        )
        (cdir / "src" / "aisquare_pipe_kit" / "connector.py").unlink()
        (cdir / "src" / "aisquare_pipe_kit" / "toolkit.py").write_text(
            'class KitToolkit:\n    version = "0.1.0"\n', encoding="utf-8"
        )
        results = run(root, "kit", DistInfo(found=True, version="0.5.0"))
        assert _errors(results) == []
        assert _ok(results, "hygiene.entry-points-registered")

    def test_toolkit_package_needs_toolkit_compliance_suite(self, tmp_path):
        root = build_repo(tmp_path)
        build_connector(root, "kit", entry_points=TOOLKIT_ONLY)  # connector suite only
        result = run(root, "kit")["hygiene.scaffold-files"]
        assert not result.passed and "toolkit_compliance_suite" in result.message

    def test_connector_and_toolkit_need_both_suites(self, tmp_path):
        root = build_repo(tmp_path)
        build_connector(root, "both", entry_points=BOTH)
        result = run(root, "both")["hygiene.scaffold-files"]
        assert not result.passed
        assert "toolkit_compliance_suite" in result.message
        assert "connector_compliance_suite" not in result.message

        both_suites = (
            "from x import connector_compliance_suite, toolkit_compliance_suite\n"
            "connector_compliance_suite(object)\ntoolkit_compliance_suite(object)\n"
        )
        (root / "connectors" / "both" / "tests" / "test_compliance.py").write_text(
            both_suites, encoding="utf-8"
        )
        assert _ok(run(root, "both"), "hygiene.scaffold-files")

    def test_package_without_any_entry_points_keeps_connector_rules(self, tmp_path):
        root = build_repo(tmp_path)
        cdir = build_connector(root, "bad", entry_points={})
        (cdir / "src" / "aisquare_pipe_bad" / "connector.py").unlink()
        results = run(root, "bad")
        assert not _ok(results, "hygiene.entry-points-registered")
        assert "connector.py" in results["hygiene.scaffold-files"].message


class TestSourceCode:
    def test_cross_connector_import(self, tmp_path):
        root = build_repo(tmp_path)
        build_connector(
            root,
            "bad",
            connector_py="from aisquare_pipe_good.connector import GoodSource\n",
        )
        result = run(root, "bad")["hygiene.no-cross-connector-imports"]
        assert not result.passed and "aisquare_pipe_good" in result.message

    def test_own_module_import_allowed(self, tmp_path):
        root = build_repo(tmp_path)
        build_connector(
            root,
            "bad",
            connector_py="from aisquare_pipe_bad import constants  # noqa\n",
        )
        assert _ok(run(root, "bad"), "hygiene.no-cross-connector-imports")

    def test_bare_except(self, tmp_path):
        root = build_repo(tmp_path)
        build_connector(
            root,
            "bad",
            connector_py="try:\n    pass\nexcept:\n    pass\n",
        )
        result = run(root, "bad")["hygiene.no-bare-except"]
        assert not result.passed and "connector.py:3" in result.message

    def test_tests_dir_not_scanned(self, tmp_path):
        root = build_repo(tmp_path)
        cdir = build_connector(root, "bad")
        (cdir / "tests" / "test_x.py").write_text(
            "from aisquare_pipe_good import x\n", encoding="utf-8"
        )
        assert _ok(run(root, "bad"), "hygiene.no-cross-connector-imports")


class TestVersions:
    """A connector's classes carry its own contract version, not the
    distribution's: one version per connector, any value."""

    def test_classes_of_one_connector_must_agree_via_source_scan(self, tmp_path):
        root = build_repo(tmp_path)
        build_connector(
            root,
            "bad",
            connector_py=(
                'class BadSource:\n    version = "0.1.0"\n\n\n'
                'class BadSink:\n    version = "9.9.9"\n'
            ),
        )
        result = run(root, "bad")["hygiene.version-sync"]
        assert not result.passed and "9.9.9" in result.message

    def test_a_connector_version_need_not_match_the_distribution(self, tmp_path):
        root = build_repo(tmp_path)
        build_connector(
            root, "old", connector_py='class OldSource:\n    version = "0.1.0"\n'
        )
        results = run(root, "old", DistInfo(found=True, version="0.5.0"))
        assert _ok(results, "hygiene.version-sync") and _ok(
            results, "hygiene.version-set"
        )

    def test_version_sync_via_loaded_classes(self, tmp_path):
        root = build_repo(tmp_path)
        cdir = get_cdir(root, "good")

        class FakeSource:
            version = "2.0.0"

        class FakeToolkit:
            version = "2.1.0"

        for cls in (FakeSource, FakeToolkit):
            cls.__module__ = "aisquare_pipe_good.connector"
        results = {
            r.id: r
            for r in run_hygiene_checks(
                cdir, root, DistInfo(found=False), {"a": FakeSource, "b": FakeToolkit}
            )
        }
        assert not results["hygiene.version-sync"].passed

    def test_no_version_anywhere_warns(self, tmp_path):
        root = build_repo(tmp_path)
        build_connector(root, "bad", connector_py="class BadSource:\n    pass\n")
        result = run(root, "bad")["hygiene.version-set"]
        assert not result.passed and result.severity is Severity.WARNING

    def test_stale_framework_install_warns(self, tmp_path, monkeypatch):
        root = build_repo(tmp_path)
        monkeypatch.setattr(
            hygiene, "_framework_dist_facts", lambda: (True, "0.4.9", None)
        )
        (stale,) = check_framework_install(root)
        assert stale.id == "hygiene.dist-version-fresh"
        assert not stale.passed and stale.severity is Severity.WARNING
        monkeypatch.setattr(
            hygiene, "_framework_dist_facts", lambda: (True, "0.5.0", None)
        )
        assert check_framework_install(root) == []


class TestTier:
    def test_a_connector_missing_from_full_fails(self, tmp_path):
        root = build_repo(tmp_path)
        build_connector(root, "niche", in_full=False)
        result = run(root, "niche")["hygiene.tier-listed"]
        assert not result.passed and result.severity is Severity.ERROR


class TestLiveMarkers:
    def test_absent_live_file_no_checks(self, tmp_path):
        root = build_repo(tmp_path)
        assert "hygiene.live-marker-registered" not in run(root, "good")

    def test_live_file_without_marker_fails(self, tmp_path):
        root = build_repo(tmp_path)
        cdir = build_connector(root, "bad")
        (cdir / "tests" / "test_live.py").write_text("# live\n", encoding="utf-8")
        assert not _ok(run(root, "bad"), "hygiene.live-marker-registered")

    def test_live_file_with_marker_passes(self, tmp_path):
        root = build_repo(tmp_path)
        pyproject = (
            "[tool.pytest.ini_options]\n"
            'markers = ["live: hits real APIs"]\n'
            "addopts = \"-m 'not live'\"\n"
        )
        cdir = build_connector(root, "ok", pyproject=pyproject)
        (cdir / "tests" / "test_live.py").write_text("# live\n", encoding="utf-8")
        results = run(root, "ok")
        assert _ok(results, "hygiene.live-marker-registered")
        assert _ok(results, "hygiene.live-deselected-by-default")


def _installed(cdir: ConnectorDir, **kw) -> DistInfo:
    return DistInfo(
        found=True,
        version="0.5.0",
        installed_entry_points=cdir.declared_entry_points(),
        **kw,
    )


class TestInstallState:
    @pytest.fixture
    def cdir(self, tmp_path) -> ConnectorDir:
        return get_cdir(build_repo(tmp_path), "good")

    def test_framework_not_installed(self, cdir):
        assert classify_install_state(cdir, DistInfo(found=False)) == INSTALL_MISSING
        result = check_install_state(cdir, DistInfo(found=False))[0]
        assert not result.passed and "aisquare-pipe is not installed" in result.message
        assert DEV_INSTALL in result.message

    def test_extra_not_installed_is_missing(self, cdir):
        dist = _installed(cdir, missing_requirements=["good-sdk"])
        assert classify_install_state(cdir, dist) == INSTALL_MISSING
        result = check_install_state(cdir, dist)[0]
        assert "aisquare-pipe[good] is missing good-sdk" in result.message

    def test_ok_editable_install_of_this_checkout(self, cdir):
        dist = _installed(cdir, editable_path=cdir.path.parents[1])
        assert classify_install_state(cdir, dist) == INSTALL_OK

    def test_broken_install_of_another_checkout(self, cdir, tmp_path):
        dist = _installed(cdir, editable_path=tmp_path / "old-repo")
        assert classify_install_state(cdir, dist) == INSTALL_BROKEN
        result = check_install_state(cdir, dist)[0]
        assert not result.passed and "not this checkout" in result.message

    def test_broken_ep_load_errors(self, cdir):
        dist = _installed(cdir, ep_load_errors={"good-source": "SyntaxError"})
        assert classify_install_state(cdir, dist) == INSTALL_BROKEN

    def test_declared_but_not_installed_entry_points_are_stale(self, cdir):
        dist = DistInfo(found=True, version="0.5.0", installed_entry_points=set())
        assert classify_install_state(cdir, dist) == INSTALL_BROKEN
        result = check_install_state(cdir, dist)[0]
        assert "good-source are declared but not installed" in result.message

    def test_install_from_a_wheel_is_ok(self, cdir):
        assert classify_install_state(cdir, _installed(cdir)) == INSTALL_OK


def _ep(
    name: str, value: str, *, error: str | None = None, missing=()
) -> DiscoveredConnector:
    return DiscoveredConnector(
        ep_name=name,
        ep_value=value,
        dist_name="aisquare-pipe",
        dist_version="0.5.0",
        cls=None if error else object,
        load_error=error,
        missing_requirements=tuple(missing),
    )


class TestLoadDistInfo:
    def test_attributes_entry_points_by_module_and_separates_missing_extras(
        self, tmp_path, monkeypatch
    ):
        root = build_repo(tmp_path)
        build_connector(root, "sdk", deps=["sdk-lib>=1"])
        monkeypatch.setattr(
            hygiene, "_framework_dist_facts", lambda: (True, "0.5.0", root)
        )
        discovered = [
            _ep("good-source", "aisquare_pipe_good.connector:GoodSource"),
            _ep(
                "sdk-source",
                "aisquare_pipe_sdk.connector:SdkSource",
                error="ModuleNotFoundError: No module named 'sdk_lib'",
                missing=["sdk-lib"],
            ),
            _ep("mock-source", "aisquare.pipe.testing.mock_connectors:MockSource"),
        ]
        good = load_dist_info(get_cdir(root, "good"), discovered)
        assert (
            good.installed_entry_points == {"good-source"} and good.ep_load_errors == {}
        )
        sdk = load_dist_info(get_cdir(root, "sdk"), discovered)
        assert sdk.missing_requirements == ["sdk-lib"] and sdk.ep_load_errors == {}
        assert classify_install_state(get_cdir(root, "sdk"), sdk) == INSTALL_MISSING

    def test_framework_not_installed(self, tmp_path, monkeypatch):
        root = build_repo(tmp_path)
        monkeypatch.setattr(
            hygiene, "_framework_dist_facts", lambda: (False, None, None)
        )
        assert load_dist_info(get_cdir(root, "good"), []) == DistInfo(found=False)


class TestFileUrlToPath:
    """``direct_url.json`` records an editable install as a file:// URL. Getting
    this wrong makes every editable connector look like a broken install.

    ``url2pathname`` is itself platform-specific — ``nturl2path`` on Windows,
    plain ``unquote`` elsewhere — so the cross-platform assertions here are
    about the round-trip, and the drive-letter case is guarded."""

    def test_roundtrips_a_real_directory(self, tmp_path):
        """The invariant that actually decides install-state, on every OS."""
        target = tmp_path / "connectors" / "good"
        target.mkdir(parents=True)
        assert _file_url_to_path(target.as_uri()).resolve() == target.resolve()

    def test_roundtrips_a_path_containing_a_space(self, tmp_path):
        target = tmp_path / "my repo" / "connectors" / "good"
        target.mkdir(parents=True)
        # as_uri() percent-escapes the space; the old textual strip left it in.
        assert "%20" in target.as_uri()
        assert _file_url_to_path(target.as_uri()).resolve() == target.resolve()

    def test_percent_escapes_are_decoded(self):
        assert _file_url_to_path("file:///tmp/my%20repo").name == "my repo"

    @pytest.mark.skipif(os.name != "nt", reason="drive letters are Windows-only")
    def test_windows_drive_url_has_no_leading_slash(self):
        # pip writes three slashes, so the path component is "/N:/repo/x".
        assert _file_url_to_path("file:///N:/repo/x").as_posix().startswith("N:")

    @pytest.mark.skipif(os.name == "nt", reason="POSIX path semantics")
    def test_posix_url_is_unchanged(self):
        assert _file_url_to_path("file:///home/u/repo/x") == Path("/home/u/repo/x")

    def test_posix_behaviour_matches_the_old_strip_for_plain_paths(self, monkeypatch):
        """On POSIX ``url2pathname`` is just ``unquote``, so ordinary paths must
        resolve exactly as the previous textual strip did — this fix may only
        change what was already wrong (escapes, Windows drives)."""
        monkeypatch.setattr(hygiene, "url2pathname", unquote)
        for url in (
            "file:///home/u/repo/connectors/x",
            "file:///var/lib/pipe/connectors/local",
            "file:///srv/a-b_c.d/connectors/n8n",
        ):
            assert _file_url_to_path(url) == Path(url.removeprefix("file://"))
