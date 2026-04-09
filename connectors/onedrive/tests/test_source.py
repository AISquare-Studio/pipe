"""Tests for OneDriveSource connector."""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest

from aisquare.pipe.core.envelope import PullParams, RateLimit

from aisquare_pipe_onedrive.connector import OneDriveSource

from tests.helpers import make_file_item, make_folder_item


@pytest.fixture
def mock_client():
    """Patch OneDriveClient and return the mock instance."""
    with patch("aisquare_pipe_onedrive.connector.OneDriveClient") as mock_cls:
        mock_instance = MagicMock()
        mock_cls.return_value = mock_instance
        yield mock_instance


class TestOneDriveSourcePull:
    def test_yields_envelopes(self, mock_client):
        files = [
            make_file_item("a.txt", "id-1", 100, "text/plain"),
            make_file_item("b.pdf", "id-2", 200, "application/pdf"),
        ]
        mock_client.list_folder.return_value = iter(files)
        mock_client.download.side_effect = [
            (files[0], b"content-a"),
            (files[1], b"content-b"),
        ]

        source = OneDriveSource()
        envelopes = list(source.pull({"access_token": "tok"}))
        assert len(envelopes) == 2
        assert envelopes[0].content_type == "text/plain"
        assert envelopes[0].data == b"content-a"
        assert envelopes[0].metadata["filename"] == "a.txt"
        assert envelopes[1].content_type == "application/pdf"

    def test_skips_folders(self, mock_client):
        items = [
            make_folder_item("subdir"),
            make_file_item("file.txt", "id-1", 50, "text/plain"),
        ]
        mock_client.list_folder.return_value = iter(items)
        mock_client.download.return_value = (items[1], b"data")

        source = OneDriveSource()
        envelopes = list(source.pull({"access_token": "tok"}))
        assert len(envelopes) == 1

    def test_extension_filter(self, mock_client):
        files = [
            make_file_item("a.txt", "id-1", 10, "text/plain"),
            make_file_item("b.pdf", "id-2", 20, "application/pdf"),
            make_file_item("c.txt", "id-3", 30, "text/plain"),
        ]
        mock_client.list_folder.return_value = iter(files)
        mock_client.download.side_effect = [
            (files[0], b"a"),
            (files[2], b"c"),
        ]

        source = OneDriveSource()
        params = PullParams(params={"extensions": [".txt"]})
        envelopes = list(source.pull({"access_token": "tok"}, params))
        assert len(envelopes) == 2
        assert all(e.metadata["filename"].endswith(".txt") for e in envelopes)

    def test_limit(self, mock_client):
        files = [make_file_item(f"f{i}.txt", f"id-{i}", 10, "text/plain") for i in range(5)]
        mock_client.list_folder.return_value = iter(files)
        mock_client.download.side_effect = [(f, b"data") for f in files]

        source = OneDriveSource()
        params = PullParams(params={"limit": 2})
        envelopes = list(source.pull({"access_token": "tok"}, params))
        assert len(envelopes) == 2

    def test_large_file_uses_stream(self, mock_client):
        big_file = make_file_item("big.zip", "id-big", 100 * 1024 * 1024, "application/zip")
        mock_client.list_folder.return_value = iter([big_file])
        mock_client.download_stream.return_value = (big_file, io.BytesIO(b"stream-data"))

        source = OneDriveSource()
        params = PullParams(params={"stream_threshold": 50 * 1024 * 1024})
        envelopes = list(source.pull({"access_token": "tok"}, params))
        assert len(envelopes) == 1
        assert envelopes[0].stream is not None
        assert envelopes[0].data == b""
        mock_client.download_stream.assert_called_once()

    def test_mime_type_from_graph_api(self, mock_client):
        """OneDrive provides MIME type in file metadata — should use it."""
        files = [
            make_file_item("photo.jpg", "id-1", 100, "image/jpeg"),
            make_file_item("data.csv", "id-2", 50, "text/csv"),
        ]
        mock_client.list_folder.return_value = iter(files)
        mock_client.download.side_effect = [(f, b"data") for f in files]

        source = OneDriveSource()
        envelopes = list(source.pull({"access_token": "tok"}))
        assert envelopes[0].content_type == "image/jpeg"
        assert envelopes[1].content_type == "text/csv"

    def test_metadata_fields(self, mock_client):
        f = make_file_item("doc.pdf", "id-x", 1024, "application/pdf")
        mock_client.list_folder.return_value = iter([f])
        mock_client.download.return_value = (f, b"pdf-bytes")

        source = OneDriveSource()
        envelopes = list(source.pull({"access_token": "tok"}))
        meta = envelopes[0].metadata
        assert meta["filename"] == "doc.pdf"
        assert meta["size"] == 1024
        assert meta["item_id"] == "id-x"
        assert meta["mime_type"] == "application/pdf"
        assert "last_modified" in meta
        assert "web_url" in meta

    def test_recursive_param(self, mock_client):
        mock_client.list_folder.return_value = iter([])
        source = OneDriveSource()
        params = PullParams(params={"recursive": True})
        list(source.pull({"access_token": "tok"}, params))
        mock_client.list_folder.assert_called_once_with("", recursive=True)


class TestOneDriveSourceValidateConfig:
    def test_valid_access_token(self, mock_client):
        mock_client.validate.return_value = True
        source = OneDriveSource()
        assert source.validate_config({"access_token": "tok"}) is True

    def test_valid_client_credentials(self, mock_client):
        mock_client.validate.return_value = True
        source = OneDriveSource()
        config = {"client_id": "c", "client_secret": "s", "tenant_id": "t"}
        assert source.validate_config(config) is True

    def test_missing_keys(self):
        source = OneDriveSource()
        assert source.validate_config({}) is False

    def test_validation_exception_returns_false(self, mock_client):
        mock_client.validate.side_effect = Exception("network error")
        source = OneDriveSource()
        assert source.validate_config({"access_token": "tok"}) is False


class TestOneDriveSourceListResources:
    def test_returns_resources(self, mock_client):
        items = [
            make_folder_item("Documents", "folder-1"),
            make_file_item("readme.md", "file-1", 500, "text/markdown"),
        ]
        mock_client.list_folder.return_value = iter(items)

        source = OneDriveSource()
        resources = source.list_resources({"access_token": "tok"})
        assert len(resources) == 2
        assert resources[0].resource_type == "folder"
        assert resources[0].name == "Documents"
        assert resources[1].resource_type == "file"
        assert resources[1].name == "readme.md"


class TestOneDriveSourceAttributes:
    def test_supports_streaming(self):
        assert OneDriveSource().supports_streaming() is True

    def test_rate_limit(self):
        rl = OneDriveSource().rate_limit()
        assert isinstance(rl, RateLimit)
        assert rl.requests_per_minute == 600
