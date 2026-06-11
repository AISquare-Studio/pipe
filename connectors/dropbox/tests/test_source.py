"""Tests for DropboxSource connector."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from aisquare.pipe.core.envelope import PullParams, RateLimit

from aisquare_pipe_dropbox.connector import DropboxSource

from tests.helpers import make_file_metadata, make_folder_metadata


@pytest.fixture
def mock_client():
    """Patch DropboxClient and return the mock instance."""
    with patch("aisquare_pipe_dropbox.connector.DropboxClient") as mock_cls:
        mock_instance = MagicMock()
        mock_cls.return_value = mock_instance
        yield mock_instance


class TestDropboxSourcePull:
    def test_yields_envelopes(self, mock_client):
        files = [
            make_file_metadata("a.txt", "/a.txt", size=100),
            make_file_metadata("b.pdf", "/b.pdf", size=200),
        ]
        mock_client.list_folder.return_value = iter(files)
        mock_client.download.side_effect = [
            (files[0], b"content-a"),
            (files[1], b"content-b"),
        ]

        source = DropboxSource()
        envelopes = list(source.pull({"access_token": "tok"}))
        assert len(envelopes) == 2
        assert envelopes[0].content_type == "text/plain"
        assert envelopes[0].data == b"content-a"
        assert envelopes[0].metadata["filename"] == "a.txt"
        assert envelopes[1].content_type == "application/pdf"

    def test_skips_folders(self, mock_client):
        entries = [
            make_folder_metadata("subdir", "/subdir"),
            make_file_metadata("file.txt", "/file.txt", size=50),
        ]
        mock_client.list_folder.return_value = iter(entries)
        mock_client.download.return_value = (entries[1], b"data")

        source = DropboxSource()
        envelopes = list(source.pull({"access_token": "tok"}))
        assert len(envelopes) == 1

    def test_extension_filter(self, mock_client):
        files = [
            make_file_metadata("a.txt", "/a.txt", size=10),
            make_file_metadata("b.pdf", "/b.pdf", size=20),
            make_file_metadata("c.txt", "/c.txt", size=30),
        ]
        mock_client.list_folder.return_value = iter(files)
        mock_client.download.side_effect = [
            (files[0], b"a"),
            (files[2], b"c"),
        ]

        source = DropboxSource()
        params = PullParams(params={"extensions": [".txt"]})
        envelopes = list(source.pull({"access_token": "tok"}, params))
        assert len(envelopes) == 2
        assert all(e.metadata["filename"].endswith(".txt") for e in envelopes)

    def test_limit(self, mock_client):
        files = [make_file_metadata(f"f{i}.txt", f"/f{i}.txt", size=10) for i in range(5)]
        mock_client.list_folder.return_value = iter(files)
        mock_client.download.side_effect = [(f, b"data") for f in files]

        source = DropboxSource()
        params = PullParams(params={"limit": 2})
        envelopes = list(source.pull({"access_token": "tok"}, params))
        assert len(envelopes) == 2

    def test_large_file_uses_stream(self, mock_client):
        big_file = make_file_metadata("big.zip", "/big.zip", size=100 * 1024 * 1024)
        mock_client.list_folder.return_value = iter([big_file])

        import io

        mock_client.download_stream.return_value = (big_file, io.BytesIO(b"stream-data"))

        source = DropboxSource()
        params = PullParams(params={"stream_threshold": 50 * 1024 * 1024})
        envelopes = list(source.pull({"access_token": "tok"}, params))
        assert len(envelopes) == 1
        assert envelopes[0].stream is not None
        assert envelopes[0].data == b""
        mock_client.download_stream.assert_called_once()

    def test_mime_type_detection(self, mock_client):
        files = [
            make_file_metadata("photo.jpg", "/photo.jpg", size=100),
            make_file_metadata("data.csv", "/data.csv", size=50),
            make_file_metadata("noext", "/noext", size=10),
        ]
        mock_client.list_folder.return_value = iter(files)
        mock_client.download.side_effect = [(f, b"data") for f in files]

        source = DropboxSource()
        envelopes = list(source.pull({"access_token": "tok"}))
        assert envelopes[0].content_type == "image/jpeg"
        assert envelopes[1].content_type == "text/csv"
        assert envelopes[2].content_type == "application/octet-stream"

    def test_metadata_fields(self, mock_client):
        f = make_file_metadata("doc.pdf", "/docs/doc.pdf", size=1024, file_id="id:x", rev="rev1")
        mock_client.list_folder.return_value = iter([f])
        mock_client.download.return_value = (f, b"pdf-bytes")

        source = DropboxSource()
        envelopes = list(source.pull({"access_token": "tok"}))
        meta = envelopes[0].metadata
        assert meta["filename"] == "doc.pdf"
        assert meta["path"] == "/docs/doc.pdf"
        assert meta["size"] == 1024
        assert meta["rev"] == "rev1"
        assert meta["dropbox_id"] == "id:x"
        assert "server_modified" in meta

    def test_recursive_param(self, mock_client):
        mock_client.list_folder.return_value = iter([])
        source = DropboxSource()
        params = PullParams(params={"recursive": True})
        list(source.pull({"access_token": "tok"}, params))
        mock_client.list_folder.assert_called_once_with("", recursive=True)


class TestDropboxSourceValidateConfig:
    def test_valid_access_token(self, mock_client):
        mock_client.validate.return_value = True
        source = DropboxSource()
        assert source.validate_config({"access_token": "tok"}) is True

    def test_valid_refresh_token(self, mock_client):
        mock_client.validate.return_value = True
        source = DropboxSource()
        config = {"app_key": "k", "app_secret": "s", "refresh_token": "r"}
        assert source.validate_config(config) is True

    def test_missing_keys(self):
        source = DropboxSource()
        assert source.validate_config({}) is False
        assert source.validate_config({"random_key": "val"}) is False

    def test_validation_exception_returns_false(self, mock_client):
        mock_client.validate.side_effect = Exception("network error")
        source = DropboxSource()
        assert source.validate_config({"access_token": "tok"}) is False


class TestDropboxSourceListResources:
    def test_returns_resources(self, mock_client):
        entries = [
            make_folder_metadata("docs", "/docs", folder_id="id:f1"),
            make_file_metadata("readme.md", "/readme.md", size=500, file_id="id:f2"),
        ]
        mock_client.list_folder.return_value = iter(entries)

        source = DropboxSource()
        resources = source.list_resources({"access_token": "tok"})
        assert len(resources) == 2
        assert resources[0].resource_type == "folder"
        assert resources[0].name == "docs"
        assert resources[1].resource_type == "file"
        assert resources[1].name == "readme.md"


class TestDropboxSourceAttributes:
    def test_supports_streaming(self):
        assert DropboxSource().supports_streaming() is True

    def test_rate_limit(self):
        rl = DropboxSource().rate_limit()
        assert isinstance(rl, RateLimit)
        assert rl.requests_per_minute == 600
