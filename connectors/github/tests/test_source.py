"""GitHubRepoSource.pull — emission shape, skip_if_sha, tempdir lifecycle."""

from __future__ import annotations

import os
from unittest import mock

import pytest

from aisquare_pipe_github.connector import CHECKOUT_CONTENT_TYPE, GitHubRepoSource

CONFIG = {"full_name": "owner/name", "default_branch": "main", "token": "tok"}


@pytest.fixture()
def fake_client(tmp_path):
    """Patch GitHubRepoClient with a recording fake that 'clones' into tmp."""
    workdir = tmp_path / "wd"
    workdir.mkdir()

    inst = mock.Mock()
    inst.full_name = "owner/name"
    inst.branch = "main"
    inst.ls_remote_head.return_value = "deadbeef" * 5
    inst.make_workdir.return_value = (str(workdir), True)

    def _clone(wd):
        checkout = os.path.join(wd, "checkout")
        os.makedirs(checkout, exist_ok=True)
        return checkout

    inst.clone.side_effect = _clone
    inst.rev_parse_head.return_value = "a" * 40
    inst.repo_metadata.return_value = {"description": "desc", "language": "Python"}

    cleaned = []
    with mock.patch(
        "aisquare_pipe_github.connector.GitHubRepoClient"
    ) as cls:
        cls.return_value = inst
        cls.cleanup.side_effect = lambda wd: cleaned.append(wd)
        cls.shape_ok.side_effect = lambda c: bool((c or {}).get("full_name"))
        yield inst, cleaned, str(workdir)


class TestPull:
    def test_emits_exactly_one_checkout_envelope(self, fake_client):
        inst, _, _ = fake_client
        envelopes = list(GitHubRepoSource().pull(dict(CONFIG)))
        assert len(envelopes) == 1
        env = envelopes[0]
        assert env.content_type == CHECKOUT_CONTENT_TYPE
        assert env.data["path"].endswith("/checkout")
        assert env.data["head_sha"] == "a" * 40
        assert env.metadata["repo_full_name"] == "owner/name"
        assert env.metadata["head_sha"] == "a" * 40
        assert env.metadata["default_branch"] == "main"
        assert "description" not in env.metadata  # fetch_repo_metadata off by default

    def test_fetch_repo_metadata_enriches_metadata(self, fake_client):
        envelopes = list(
            GitHubRepoSource().pull({**CONFIG, "fetch_repo_metadata": True})
        )
        assert envelopes[0].metadata["description"] == "desc"
        assert envelopes[0].metadata["language"] == "Python"

    def test_skip_if_sha_yields_nothing_and_never_clones(self, fake_client):
        inst, _, _ = fake_client
        envelopes = list(
            GitHubRepoSource().pull({**CONFIG, "skip_if_sha": "deadbeef" * 5})
        )
        assert envelopes == []
        inst.clone.assert_not_called()

    def test_sha_mismatch_proceeds_to_clone(self, fake_client):
        inst, _, _ = fake_client
        envelopes = list(GitHubRepoSource().pull({**CONFIG, "skip_if_sha": "other"}))
        assert len(envelopes) == 1
        inst.clone.assert_called_once()

    def test_workdir_cleaned_after_generator_close(self, fake_client):
        _, cleaned, workdir = fake_client
        gen = GitHubRepoSource().pull(dict(CONFIG))
        next(gen)  # suspended at yield — checkout must still be alive here
        assert cleaned == []
        gen.close()  # pipeline done with the envelope
        assert cleaned == [workdir]

    def test_keep_checkout_skips_cleanup(self, fake_client):
        _, cleaned, _ = fake_client
        list(GitHubRepoSource().pull({**CONFIG, "keep_checkout": True}))
        assert cleaned == []


class TestValidateConfig:
    def test_empty_config_is_false_and_does_not_raise(self):
        assert GitHubRepoSource().validate_config({}) is False

    def test_good_shape_is_true(self):
        assert GitHubRepoSource().validate_config({"full_name": "o/r"}) is True
