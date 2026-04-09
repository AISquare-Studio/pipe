"""Tests for the DropboxClient wrapper."""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import dropbox.exceptions
import dropbox.files
import pytest

from aisquare.pipe.errors import ConfigValidationError, PipelineError

from aisquare_pipe_dropbox.client import DropboxClient

from tests.helpers import make_file_metadata, make_folder_metadata, make_list_folder_result


class TestClientInit:
    def test_init_with_access_token(self, mock_dbx, sample_config):
        client = DropboxClient(sample_config)
        assert client._dbx is not None

    def test_init_with_refresh_token(self, mock_dbx, sample_config_refresh):
        client = DropboxClient(sample_config_refresh)
        assert client._dbx is not None

    def test_init_missing_keys_raises(self):
        with pytest.raises(ConfigValidationError, match="requires either"):
            DropboxClient({"invalid": "config"})


class TestClientValidate:
    def test_validate_success(self, mock_dbx, sample_config):
        mock_dbx.users_get_current_account.return_value = MagicMock()
        client = DropboxClient(sample_config)
        assert client.validate() is True

    def test_validate_auth_error(self, mock_dbx, sample_config):
        mock_dbx.users_get_current_account.side_effect = dropbox.exceptions.AuthError(
            "req-id", MagicMock()
        )
        client = DropboxClient(sample_config)
        with pytest.raises(ConfigValidationError):
            client.validate()


class TestClientListFolder:
    def test_single_page(self, mock_dbx, sample_config):
        entries = [make_file_metadata("a.txt"), make_file_metadata("b.txt")]
        mock_dbx.files_list_folder.return_value = make_list_folder_result(
            entries, has_more=False
        )
        client = DropboxClient(sample_config)
        result = list(client.list_folder("/test"))
        assert len(result) == 2
        mock_dbx.files_list_folder.assert_called_once_with("/test", recursive=False)

    def test_pagination(self, mock_dbx, sample_config):
        page1 = make_list_folder_result(
            [make_file_metadata("a.txt")], has_more=True, cursor="c1"
        )
        page2 = make_list_folder_result(
            [make_file_metadata("b.txt")], has_more=False
        )
        mock_dbx.files_list_folder.return_value = page1
        mock_dbx.files_list_folder_continue.return_value = page2

        client = DropboxClient(sample_config)
        result = list(client.list_folder(""))
        assert len(result) == 2
        mock_dbx.files_list_folder_continue.assert_called_once_with("c1")

    def test_includes_folders(self, mock_dbx, sample_config):
        entries = [make_file_metadata("a.txt"), make_folder_metadata("subdir")]
        mock_dbx.files_list_folder.return_value = make_list_folder_result(entries)
        client = DropboxClient(sample_config)
        result = list(client.list_folder(""))
        assert len(result) == 2


class TestClientDownload:
    def test_download_returns_bytes(self, mock_dbx, sample_config):
        meta = make_file_metadata("doc.pdf")
        response = MagicMock()
        response.content = b"PDF-content"
        mock_dbx.files_download.return_value = (meta, response)

        client = DropboxClient(sample_config)
        result_meta, content = client.download("/doc.pdf")
        assert content == b"PDF-content"
        assert result_meta.name == "doc.pdf"
        response.close.assert_called_once()

    def test_download_stream(self, mock_dbx, sample_config):
        meta = make_file_metadata("big.zip")
        response = MagicMock()
        response.content = b"big-file-content"
        mock_dbx.files_download.return_value = (meta, response)

        client = DropboxClient(sample_config)
        result_meta, stream = client.download_stream("/big.zip")
        assert stream.read() == b"big-file-content"


class TestClientUpload:
    def test_simple_upload(self, mock_dbx, sample_config):
        result_meta = make_file_metadata("uploaded.txt")
        mock_dbx.files_upload.return_value = result_meta

        client = DropboxClient(sample_config)
        meta = client.upload(b"hello", "/uploaded.txt")
        assert meta.name == "uploaded.txt"
        mock_dbx.files_upload.assert_called_once()

    def test_chunked_upload(self, mock_dbx, sample_config):
        # Mock session
        session = MagicMock()
        session.session_id = "session-123"
        mock_dbx.files_upload_session_start.return_value = session
        mock_dbx.files_upload_session_finish.return_value = make_file_metadata("big.bin")

        client = DropboxClient(sample_config)
        data = b"x" * 100  # small for test, but using the chunked path
        stream = io.BytesIO(data)
        meta = client.upload_chunked(stream, "/big.bin", len(data))
        assert meta.name == "big.bin"
        mock_dbx.files_upload_session_start.assert_called_once()
        mock_dbx.files_upload_session_finish.assert_called_once()

    def test_should_chunk(self, mock_dbx, sample_config):
        client = DropboxClient(sample_config)
        assert client.should_chunk(200 * 1024 * 1024) is True  # 200MB
        assert client.should_chunk(100 * 1024 * 1024) is False  # 100MB


class TestClientErrorMapping:
    def test_api_error_mapped_to_pipeline_error(self, mock_dbx, sample_config):
        mock_dbx.files_list_folder.side_effect = dropbox.exceptions.ApiError(
            "req-id", MagicMock(), "user_message", "locale"
        )
        client = DropboxClient(sample_config)
        with pytest.raises(PipelineError, match="Dropbox API error"):
            list(client.list_folder("/test"))

    def test_auth_error_mapped_to_config_error(self, mock_dbx, sample_config):
        mock_dbx.files_upload.side_effect = dropbox.exceptions.AuthError(
            "req-id", MagicMock()
        )
        client = DropboxClient(sample_config)
        with pytest.raises(ConfigValidationError, match="auth failed"):
            client.upload(b"data", "/file.txt")


class TestClientRetry:
    def test_retry_on_429(self, mock_dbx, sample_config):
        http_err = dropbox.exceptions.HttpError("req-id", 429, b"rate limited")
        meta = make_file_metadata("ok.txt")
        response = MagicMock()
        response.content = b"ok"
        mock_dbx.files_download.side_effect = [http_err, (meta, response)]

        client = DropboxClient(sample_config)
        with patch("aisquare_pipe_dropbox.client.time.sleep"):
            result_meta, content = client.download("/ok.txt")
        assert content == b"ok"
        assert mock_dbx.files_download.call_count == 2
