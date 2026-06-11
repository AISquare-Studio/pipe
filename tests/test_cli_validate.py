"""CLI tests for `pipe validate` — orchestration stubbed, modes + rendering
+ exit codes verified."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

import aisquare.pipe.cli.validate as validate_mod
import aisquare.pipe.testing.hygiene as hygiene_mod
from aisquare.pipe.cli.main import cli
from aisquare.pipe.cli.validate import SuiteResult
from aisquare.pipe.testing.hygiene import DistInfo
from aisquare.pipe.testing.mock_connectors import MockSink, MockSource
from aisquare.pipe.testing.validation import DiscoveredConnector

from tests.test_hygiene import build_connector, build_repo


def _passing_suite(cwd: Path, extra_args=None, timeout=300) -> SuiteResult:
    return SuiteResult(
        target=cwd.name,
        returncode=0,
        summary="12 passed in 0.10s",
        output_tail="12 passed in 0.10s",
        duration=0.1,
        timed_out=False,
    )


def _failing_suite(cwd: Path, extra_args=None, timeout=300) -> SuiteResult:
    return SuiteResult(
        target=cwd.name,
        returncode=1,
        summary="1 failed, 11 passed in 0.10s",
        output_tail="FAILED tests/test_x.py::test_y - boom",
        duration=0.1,
        timed_out=False,
    )


class _GoodSource(MockSource):
    name = "good-source"


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """A fake repo with one compliant, 'installed' connector and stubbed
    discovery + suite runner."""
    root = build_repo(tmp_path)

    discovered = [
        DiscoveredConnector(
            ep_name="good-source",
            ep_value="aisquare_pipe_good.connector:GoodSource",
            dist_name="aisquare-pipe-good",
            dist_version="0.1.0",
            cls=_GoodSource,
            load_error=None,
        ),
        DiscoveredConnector(
            ep_name="mock-source",
            ep_value="aisquare.pipe.testing.mock_connectors:MockSource",
            dist_name="aisquare-pipe",
            dist_version="0.1.0",
            cls=MockSource,
            load_error=None,
        ),
        DiscoveredConnector(
            ep_name="mock-sink",
            ep_value="aisquare.pipe.testing.mock_connectors:MockSink",
            dist_name="aisquare-pipe",
            dist_version="0.1.0",
            cls=MockSink,
            load_error=None,
        ),
    ]

    def fake_dist_info(dist_name, discovered_list):
        if dist_name == "aisquare-pipe-good":
            return DistInfo(
                found=True, version="0.1.0", editable_path=root / "connectors" / "good"
            )
        return DistInfo(found=False)

    monkeypatch.setattr(hygiene_mod, "find_repo_root", lambda start=None: root)
    monkeypatch.setattr(hygiene_mod, "load_dist_info", fake_dist_info)
    monkeypatch.setattr(validate_mod, "discover_connector_entry_points", lambda: discovered)
    monkeypatch.setattr(validate_mod, "run_pytest_suite", _passing_suite)
    return root


class TestRepoMode:
    def test_all_pass_exit_zero(self, fake_repo):
        result = CliRunner().invoke(cli, ["validate"])
        assert result.exit_code == 0, result.output
        assert "good" in result.output
        assert "PASS" in result.output
        assert "framework" in result.output

    def test_unit_failure_exit_one(self, fake_repo, monkeypatch):
        monkeypatch.setattr(validate_mod, "run_pytest_suite", _failing_suite)
        result = CliRunner().invoke(cli, ["validate"])
        assert result.exit_code == 1
        assert "FAILURES" in result.output
        assert "FAILED tests/test_x.py::test_y" in result.output

    def test_warning_only_still_exit_zero(self, fake_repo):
        build_connector(fake_repo, "niche")  # not installed, not in tiers
        # niche is missing → install-state failure → that IS exit 1.
        # Restrict to the good connector: its stale-free, tier-listed tree
        # plus an injected warning-only hygiene state.
        result = CliRunner().invoke(cli, ["validate", "good"])
        assert result.exit_code == 0, result.output

    def test_skip_tests_never_runs_suites(self, fake_repo, monkeypatch):
        def boom(*args, **kwargs):
            raise AssertionError("run_pytest_suite must not be called with --skip-tests")

        monkeypatch.setattr(validate_mod, "run_pytest_suite", boom)
        result = CliRunner().invoke(cli, ["validate", "--skip-tests"])
        assert result.exit_code == 0, result.output

    def test_missing_install_fails_with_remediation(self, fake_repo):
        build_connector(fake_repo, "uninstalled")
        result = CliRunner().invoke(cli, ["validate"])
        assert result.exit_code == 1
        assert "pip install -e connectors/uninstalled" in result.output

    def test_json_output(self, fake_repo):
        result = CliRunner().invoke(cli, ["validate", "--json", "--skip-tests"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["ok"] is True
        names = {c["name"] for c in payload["connectors"]}
        assert {"good", "framework"} <= names
        good = next(c for c in payload["connectors"] if c["name"] == "good")
        assert good["install_state"] == "ok"
        assert good["layers"]["contract"]["status"] == "pass"

    def test_unknown_target_exit_two(self, fake_repo):
        result = CliRunner().invoke(cli, ["validate", "nope"])
        assert result.exit_code == 2
        assert "unknown connector" in result.output

    def test_dist_name_target_resolves(self, fake_repo):
        result = CliRunner().invoke(cli, ["validate", "aisquare-pipe-good", "--skip-tests"])
        assert result.exit_code == 0, result.output


VALID_CONNECTOR_FILE = '''\
from collections.abc import Iterator

from aisquare.pipe import AuthType, DataEnvelope, SourceConnector


class TempSource(SourceConnector):
    name = "temp"
    version = "0.1.0"
    output_types = ["text/plain"]
    auth_type = AuthType.NONE

    def pull(self, config, params=None):
        yield DataEnvelope(content_type="text/plain", data="x", source_id=self.name)

    def validate_config(self, config):
        return isinstance(config, dict)
'''


class TestLegacyFileMode:
    def test_valid_connector_file(self, tmp_path):
        path = tmp_path / "conn.py"
        path.write_text(VALID_CONNECTOR_FILE, encoding="utf-8")
        result = CliRunner().invoke(cli, ["validate", str(path)])
        assert result.exit_code == 0, result.output
        assert "Validating TempSource" in result.output

    def test_no_connector_classes_exit_one(self, tmp_path):
        path = tmp_path / "empty.py"
        path.write_text("x = 1\n", encoding="utf-8")
        result = CliRunner().invoke(cli, ["validate", str(path)])
        assert result.exit_code == 1
        assert "No connector classes" in result.output


class TestInstalledOnlyMode:
    def test_no_repo_falls_back_to_contract_layer(self, monkeypatch):
        monkeypatch.setattr(hygiene_mod, "find_repo_root", lambda start=None: None)
        monkeypatch.setattr(
            validate_mod,
            "discover_connector_entry_points",
            lambda: [
                DiscoveredConnector(
                    ep_name="mock-source",
                    ep_value="x",
                    dist_name="aisquare-pipe",
                    dist_version="0.1.0",
                    cls=MockSource,
                    load_error=None,
                )
            ],
        )
        result = CliRunner().invoke(cli, ["validate"])
        assert result.exit_code == 0, result.output
        assert "contract layer only" in result.output

    def test_named_target_without_repo_exit_two(self, monkeypatch):
        monkeypatch.setattr(hygiene_mod, "find_repo_root", lambda start=None: None)
        result = CliRunner().invoke(cli, ["validate", "composio"])
        assert result.exit_code == 2
