"""GDriveSource.pull — emission shape, filters, streaming, folder skipping."""

from __future__ import annotations

from unittest import mock

import pytest

from aisquare.pipe.core.envelope import PullParams
from aisquare.pipe.errors import ConfigValidationError

from aisquare_pipe_gdrive.connector import GDriveSource
from tests.helpers import make_file, make_folder

CONFIG = {"access_token": "test-token"}


@pytest.fixture()
def fake_client():
    """Patch GDriveClient with a recording fake."""
    inst = mock.Mock()
    inst.download.return_value = b"file-content"
    inst.download_stream.return_value = mock.MagicMock()
    with mock.patch("aisquare_pipe_gdrive.connector.GDriveClient") as cls:
        cls.return_value = inst
        yield inst


class TestPull:
    def test_emits_one_envelope_per_file(self, fake_client):
        fake_client.list_files.return_value = iter(
            [make_file(name="a.pdf"), make_file(name="b.txt", file_id="file-2")]
        )
        envelopes = list(GDriveSource().pull(dict(CONFIG)))
        assert len(envelopes) == 2

    def test_content_type_comes_from_drive_not_filename(self, fake_client):
        # Deliberately mismatched: Drive says CSV, the name says .pdf.
        fake_client.list_files.return_value = iter(
            [make_file(name="weird.pdf", mime_type="text/csv")]
        )
        env = next(iter(GDriveSource().pull(dict(CONFIG))))
        assert env.content_type == "text/csv"

    def test_metadata_shape(self, fake_client):
        fake_client.list_files.return_value = iter([make_file(name="a.pdf")])
        env = next(iter(GDriveSource().pull(dict(CONFIG))))
        assert env.metadata["filename"] == "a.pdf"
        assert env.metadata["file_id"] == "file-abc123"
        assert env.metadata["mime_type"] == "application/pdf"
        assert env.metadata["size"] == 2048
        assert env.metadata["modified_time"] == "2026-06-15T12:00:00.000Z"
        assert "exported_from" not in env.metadata

    def test_folders_are_skipped(self, fake_client):
        fake_client.list_files.return_value = iter([make_folder(), make_file()])
        envelopes = list(GDriveSource().pull(dict(CONFIG)))
        assert len(envelopes) == 1

    def test_extension_filter(self, fake_client):
        fake_client.list_files.return_value = iter(
            [make_file(name="a.pdf"), make_file(name="b.txt", file_id="file-2")]
        )
        params = PullParams(params={"extensions": [".pdf"]})
        envelopes = list(GDriveSource().pull(dict(CONFIG), params))
        assert [e.metadata["filename"] for e in envelopes] == ["a.pdf"]

    def test_glob_filter(self, fake_client):
        fake_client.list_files.return_value = iter(
            [make_file(name="report.csv"), make_file(name="a.pdf", file_id="file-2")]
        )
        params = PullParams(params={"glob": "*.csv"})
        envelopes = list(GDriveSource().pull(dict(CONFIG), params))
        assert [e.metadata["filename"] for e in envelopes] == ["report.csv"]

    def test_limit_stops_early(self, fake_client):
        fake_client.list_files.return_value = iter(
            [make_file(name=f"f{i}.pdf", file_id=f"file-{i}") for i in range(5)]
        )
        params = PullParams(params={"limit": 2})
        assert len(list(GDriveSource().pull(dict(CONFIG), params))) == 2

    def test_large_file_streams(self, fake_client):
        big = make_file(name="big.bin", size=100 * 1024 * 1024)
        fake_client.list_files.return_value = iter([big])
        env = next(iter(GDriveSource().pull(dict(CONFIG))))
        assert env.stream is not None
        assert env.data == b""
        fake_client.download.assert_not_called()

    def test_small_file_buffers(self, fake_client):
        fake_client.list_files.return_value = iter([make_file(size=1024)])
        env = next(iter(GDriveSource().pull(dict(CONFIG))))
        assert env.data == b"file-content"
        assert env.stream is None
        fake_client.download_stream.assert_not_called()

    def test_folder_id_and_recursive_forwarded(self, fake_client):
        fake_client.list_files.return_value = iter([])
        params = PullParams(params={"folder_id": "folder-9", "recursive": True})
        list(GDriveSource().pull(dict(CONFIG), params))
        fake_client.list_files.assert_called_once_with(
            folder_id="folder-9", recursive=True, query=""
        )


class TestValidateConfig:
    def test_empty_config_is_false_and_does_not_raise(self):
        assert GDriveSource().validate_config({}) is False

    def test_access_token_is_true(self):
        assert GDriveSource().validate_config({"access_token": "x"}) is True

    def test_partial_oauth_keys_is_false(self):
        assert GDriveSource().validate_config({"client_id": "x"}) is False


class TestPullNoCreds:
    def test_pull_with_empty_config_raises_clean_error(self):
        gen = GDriveSource().pull({})
        with pytest.raises(ConfigValidationError):
            next(gen)
