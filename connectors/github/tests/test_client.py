"""GitHubRepoClient — auth-env discipline + helpers.

The auth tests are ported from the backend's ``test_clone_auth.py``: the token
must reach git via the ``http.extraHeader`` Basic header in ``GIT_CONFIG_*``
env vars — NOT argv (world-readable /proc/cmdline), NOT the remote URL (leaks
into a failed clone's stderr). Subprocess is mocked throughout — no git, no
network.
"""

from __future__ import annotations

import base64
import subprocess
from unittest import mock

import pytest

from aisquare.pipe.errors import ConfigValidationError, PipelineError
from aisquare_pipe_github import client as client_mod
from aisquare_pipe_github.client import GitHubRepoClient

TOKEN = "s3cr3t-iat-token"


def _client(**overrides) -> GitHubRepoClient:
    config = {"full_name": "owner/secret-repo", "default_branch": "trunk", "token": TOKEN}
    config.update(overrides)
    return GitHubRepoClient(config)


def _ok(stdout: str = "") -> mock.Mock:
    return mock.Mock(returncode=0, stdout=stdout, stderr="")


class TestConfigShape:
    def test_missing_full_name_raises_config_error(self):
        with pytest.raises(ConfigValidationError):
            GitHubRepoClient({})

    def test_bad_full_name_raises_config_error(self):
        with pytest.raises(ConfigValidationError):
            GitHubRepoClient({"full_name": "not-a-repo"})

    def test_shape_ok_never_raises(self):
        assert GitHubRepoClient.shape_ok({}) is False
        assert GitHubRepoClient.shape_ok(None) is False
        assert GitHubRepoClient.shape_ok({"full_name": "owner/name"}) is True


class TestCloneAuthEnv:
    def _run_clone(self):
        with mock.patch.object(client_mod.subprocess, "run", return_value=_ok()) as run_mock:
            _client().clone("/tmp/workdir")
        return run_mock.call_args

    def test_token_absent_from_argv_and_url(self):
        args, kwargs = self._run_clone()
        argv = args[0]
        assert all(TOKEN not in str(a) for a in argv), f"token leaked into argv: {argv}"
        assert "https://github.com/owner/secret-repo.git" in argv
        assert "trunk" in argv

    def test_token_carried_in_extraheader_env(self):
        _, kwargs = self._run_clone()
        env = kwargs["env"]
        expected = (
            "Authorization: Basic "
            + base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
        )
        assert env["GIT_CONFIG_COUNT"] == "1"
        assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
        assert env["GIT_CONFIG_VALUE_0"] == expected
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_no_token_means_no_auth_header(self):
        with mock.patch.object(client_mod.subprocess, "run", return_value=_ok()) as run_mock:
            _client(token="").clone("/tmp/workdir")
        env = run_mock.call_args.kwargs["env"]
        assert "GIT_CONFIG_KEY_0" not in env
        assert env["GIT_TERMINAL_PROMPT"] == "0"


class TestCloneFailures:
    def test_nonzero_exit_raises_pipeline_error_with_stderr_tail(self):
        failed = mock.Mock(returncode=128, stdout="", stderr="fatal: repository not found")
        with mock.patch.object(client_mod.subprocess, "run", return_value=failed):
            with pytest.raises(PipelineError) as exc:
                _client().clone("/tmp/workdir")
        assert "exited 128" in str(exc.value)
        assert "repository not found" in str(exc.value)

    def test_timeout_raises_pipeline_error(self):
        boom = subprocess.TimeoutExpired(cmd=["git"], timeout=300, stderr=b"slow network")
        with mock.patch.object(client_mod.subprocess, "run", side_effect=boom):
            with pytest.raises(PipelineError) as exc:
                _client().clone("/tmp/workdir")
        assert "timed out" in str(exc.value)
        assert "slow network" in str(exc.value)


class TestLsRemoteAndMetadata:
    def test_ls_remote_head_parses_first_sha(self):
        out = _ok("abc123def456\trefs/heads/trunk\n")
        with mock.patch.object(client_mod.subprocess, "run", return_value=out):
            assert _client().ls_remote_head() == "abc123def456"

    def test_ls_remote_failure_returns_empty(self):
        with mock.patch.object(client_mod.subprocess, "run", side_effect=OSError("no git")):
            assert _client().ls_remote_head() == ""

    def test_repo_metadata_extracts_description_and_language(self):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"description": "A repo", "language": "Python", "junk": 1}
        with mock.patch.object(client_mod.requests, "get", return_value=resp):
            assert _client().repo_metadata() == {"description": "A repo", "language": "Python"}

    def test_repo_metadata_swallows_everything(self):
        with mock.patch.object(client_mod.requests, "get", side_effect=OSError("down")):
            assert _client().repo_metadata() == {}
