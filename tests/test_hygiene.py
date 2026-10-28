"""Tests for the Layer B hygiene checks (testing/hygiene.py) against
tmp_path fake repo trees."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote

import pytest

from aisquare.pipe.testing import hygiene
from aisquare.pipe.testing.hygiene import (
    INSTALL_BROKEN,
    INSTALL_MISSING,
    INSTALL_OK,
    ConnectorDir,
    DistInfo,
    _file_url_to_path,
    check_install_state,
    classify_install_state,
    find_repo_root,
    list_connector_dirs,
    run_hygiene_checks,
)
from aisquare.pipe.testing.validation import Severity

GOOD_PYPROJECT = """\
[project]
name = "aisquare-pipe-good"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["aisquare-pipe>=0.1.0"]

[project.entry-points."aisquare_pipe.connectors"]
good-source = "aisquare_pipe_good.connector:GoodSource"
"""

GOOD_CONNECTOR = '''\
"""Good connector."""


class GoodSource:
    name = "good-source"
    version = "0.1.0"
'''

ROOT_PYPROJECT = """\
[project]
name = "aisquare-pipe"
version = "0.1.0"

[project.optional-dependencies]
popular = ["aisquare-pipe-good"]
full = ["aisquare-pipe[popular]"]
"""


def build_repo(tmp_path: Path) -> Path:
    """A minimal fake repo with one fully-compliant connector `good`."""
    root = tmp_path / "repo"
    (root / "connectors").mkdir(parents=True)
    (root / "pyproject.toml").write_text(ROOT_PYPROJECT, encoding="utf-8")
    build_connector(root, "good")
    return root


def build_connector(
    root: Path,
    name: str,
    *,
    pyproject: str | None = None,
    connector_py: str | None = None,
    compliance: str | None = "from x import connector_compliance_suite\nconnector_compliance_suite(object)\n",
) -> Path:
    cdir = root / "connectors" / name
    safe = name.replace("-", "_")
    module = cdir / "src" / f"aisquare_pipe_{safe}"
    module.mkdir(parents=True)
    (cdir / "tests").mkdir()
    (cdir / "pyproject.toml").write_text(
        pyproject if pyproject is not None else GOOD_PYPROJECT.replace("good", name),
        encoding="utf-8",
    )
    (cdir / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    (module / "__init__.py").write_text("", encoding="utf-8")
    (module / "connector.py").write_text(
        connector_py if connector_py is not None else GOOD_CONNECTOR.replace("good", name),
        encoding="utf-8",
    )
    (cdir / "tests" / "__init__.py").write_text("", encoding="utf-8")
    if compliance is not None:
        (cdir / "tests" / "test_compliance.py").write_text(compliance, encoding="utf-8")
    return cdir


def get_cdir(root: Path, name: str) -> ConnectorDir:
    return next(c for c in list_connector_dirs(root) if c.dir_name == name)


def run(root: Path, name: str, dist: DistInfo | None = None) -> dict[str, object]:
    cdir = get_cdir(root, name)
    results = run_hygiene_checks(cdir, root, dist or DistInfo(found=False), {})
    return {r.id: r for r in results}


def _ok(results: dict, check_id: str) -> bool:
    return results[check_id].passed  # type: ignore[union-attr]


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
        assert cdir.dist_name == "aisquare-pipe-my-svc"


class TestGoodTreePasses:
    def test_no_error_failures(self, tmp_path):
        root = build_repo(tmp_path)
        results = run(root, "good", DistInfo(found=True, version="0.1.0"))
        errors = [
            r for r in results.values() if not r.passed and r.severity is Severity.ERROR
        ]
        assert errors == []

    def test_tier_listed_passes_for_popular(self, tmp_path):
        root = build_repo(tmp_path)
        assert _ok(run(root, "good"), "hygiene.tier-listed")


class TestPyprojectRules:
    def test_wrong_package_name(self, tmp_path):
        root = build_repo(tmp_path)
        build_connector(
            root, "bad", pyproject=GOOD_PYPROJECT.replace(
                'name = "aisquare-pipe-good"', 'name = "aisquare_bad"'
            )
        )
        assert not _ok(run(root, "bad"), "hygiene.package-name")

    def test_missing_framework_dep(self, tmp_path):
        root = build_repo(tmp_path)
        build_connector(
            root, "bad", pyproject=GOOD_PYPROJECT.replace("good", "bad").replace(
                'dependencies = ["aisquare-pipe>=0.1.0"]', "dependencies = []"
            )
        )
        assert not _ok(run(root, "bad"), "hygiene.framework-dep")

    def test_missing_entry_points(self, tmp_path):
        root = build_repo(tmp_path)
        bad = GOOD_PYPROJECT.replace("good", "bad").split("[project.entry-points")[0]
        build_connector(root, "bad", pyproject=bad)
        assert not _ok(run(root, "bad"), "hygiene.entry-points-registered")

    def test_missing_requires_python(self, tmp_path):
        root = build_repo(tmp_path)
        build_connector(
            root, "bad", pyproject=GOOD_PYPROJECT.replace("good", "bad").replace(
                'requires-python = ">=3.11"\n', ""
            )
        )
        assert not _ok(run(root, "bad"), "hygiene.requires-python")

    def test_unparseable_pyproject(self, tmp_path):
        root = build_repo(tmp_path)
        build_connector(root, "bad", pyproject="not [ valid toml")
        assert not _ok(run(root, "bad"), "hygiene.pyproject-parses")


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
        (cdir / "src" / "aisquare_pipe_bad").rename(cdir / "src" / "aisquare_pipe_other")
        assert not _ok(run(root, "bad"), "hygiene.module-name")


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
    def test_version_sync_mismatch_via_source_scan(self, tmp_path):
        root = build_repo(tmp_path)
        build_connector(
            root,
            "bad",
            connector_py='class BadSource:\n    name = "bad"\n    version = "9.9.9"\n',
        )
        result = run(root, "bad")["hygiene.version-sync"]
        assert not result.passed and "9.9.9" in result.message

    def test_version_sync_via_loaded_class(self, tmp_path):
        root = build_repo(tmp_path)
        cdir = get_cdir(root, "good")

        class FakeLoaded:
            version = "2.0.0"

        FakeLoaded.__module__ = "aisquare_pipe_good.connector"
        results = {
            r.id: r
            for r in run_hygiene_checks(
                cdir, root, DistInfo(found=False), {"good-source": FakeLoaded}
            )
        }
        assert not results["hygiene.version-sync"].passed

    def test_stale_dist_version_warns(self, tmp_path):
        root = build_repo(tmp_path)
        results = run(root, "good", DistInfo(found=True, version="0.0.9"))
        stale = results["hygiene.dist-version-fresh"]
        assert not stale.passed and stale.severity is Severity.WARNING


class TestTier:
    def test_unlisted_connector_warns(self, tmp_path):
        root = build_repo(tmp_path)
        build_connector(root, "niche")
        result = run(root, "niche")["hygiene.tier-listed"]
        assert not result.passed and result.severity is Severity.WARNING


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
        pyproject = GOOD_PYPROJECT.replace("good", "ok") + (
            "\n[tool.pytest.ini_options]\n"
            'markers = ["live: hits real APIs"]\n'
            "addopts = \"-m 'not live'\"\n"
        )
        cdir = build_connector(root, "ok", pyproject=pyproject)
        (cdir / "tests" / "test_live.py").write_text("# live\n", encoding="utf-8")
        results = run(root, "ok")
        assert _ok(results, "hygiene.live-marker-registered")
        assert _ok(results, "hygiene.live-deselected-by-default")


class TestInstallState:
    @pytest.fixture
    def cdir(self, tmp_path) -> ConnectorDir:
        return get_cdir(build_repo(tmp_path), "good")

    def test_missing(self, cdir):
        assert classify_install_state(cdir, DistInfo(found=False)) == INSTALL_MISSING
        result = check_install_state(cdir, DistInfo(found=False))[0]
        assert not result.passed and "pip install -e connectors/good" in result.message

    def test_ok_editable_path_matches(self, cdir):
        dist = DistInfo(found=True, version="0.1.0", editable_path=cdir.path)
        assert classify_install_state(cdir, dist) == INSTALL_OK

    def test_broken_editable_path_elsewhere(self, cdir, tmp_path):
        dist = DistInfo(found=True, version="0.1.0", editable_path=tmp_path / "old" / "good")
        assert classify_install_state(cdir, dist) == INSTALL_BROKEN
        result = check_install_state(cdir, dist)[0]
        assert not result.passed and "repo moved" in result.message

    def test_broken_ep_load_errors(self, cdir):
        dist = DistInfo(found=True, version="0.1.0", ep_load_errors={"good-source": "ModuleNotFoundError"})
        assert classify_install_state(cdir, dist) == INSTALL_BROKEN

    def test_non_editable_install_ok(self, cdir):
        assert classify_install_state(cdir, DistInfo(found=True, version="0.1.0")) == INSTALL_OK


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
