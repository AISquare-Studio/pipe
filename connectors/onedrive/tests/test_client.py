"""Tests for the OneDriveClient wrapper."""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, patch

import pytest

from aisquare.pipe.errors import ConfigValidationError, PipelineError

from aisquare_pipe_onedrive.client import OneDriveClient

from tests.helpers import make_file_item, make_graph_response, make_upload_response


def _mock_response(status_code=200, json_data=None, content=b"", headers=None):
    """Create a mock requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.content = content
    resp.text = json.dumps(json_data) if json_data else content.decode("utf-8", errors="replace")
    resp.headers = headers or {}
    return resp


class TestClientInit:
    def test_init_with_access_token(self):
        client = OneDriveClient({"access_token": "tok"})
        assert client._token == "tok"

    def test_init_with_client_credentials(self):
        with patch("aisquare_pipe_onedrive.client.OneDriveClient._acquire_token_client_credentials") as mock_acq:
            mock_acq.return_value = "acquired-token"
            config = {"client_id": "c", "client_secret": "s", "tenant_id": "t"}
            client = OneDriveClient(config)
            assert client._token == "acquired-token"

    def test_init_missing_keys_raises(self):
        with pytest.raises(ConfigValidationError, match="requires either"):
            OneDriveClient({"invalid": "config"})


class TestClientValidate:
    def test_validate_success(self, mock_requests, sample_config):
        mock_requests.return_value = _mock_response(200, {"id": "drive-123"})
        client = OneDriveClient(sample_config)
        assert client.validate() is True

    def test_validate_auth_error(self, mock_requests, sample_config):
        mock_requests.return_value = _mock_response(401, {}, headers={})
        client = OneDriveClient(sample_config)
        with pytest.raises(ConfigValidationError, match="401"):
            client.validate()


class TestClientListFolder:
    def test_single_page(self, mock_requests, sample_config):
        items = [make_file_item("a.txt"), make_file_item("b.txt")]
        mock_requests.return_value = _mock_response(200, make_graph_response(items))

        client = OneDriveClient(sample_config)
        result = list(client.list_folder("/"))
        assert len(result) == 2

    def test_pagination(self, mock_requests, sample_config):
        page1 = make_graph_response([make_file_item("a.txt")], next_link="https://next")
        page2 = make_graph_response([make_file_item("b.txt")])
        mock_requests.side_effect = [
            _mock_response(200, page1),
            _mock_response(200, page2),
        ]

        client = OneDriveClient(sample_config)
        result = list(client.list_folder("/"))
        assert len(result) == 2

    def test_custom_path(self, mock_requests, sample_config):
        mock_requests.return_value = _mock_response(200, make_graph_response([]))
        client = OneDriveClient(sample_config)
        list(client.list_folder("/Documents/Reports"))
        call_url = mock_requests.call_args[0][1]
        assert "Documents/Reports" in call_url


class TestClientDownload:
    def test_download_returns_bytes(self, mock_requests, sample_config):
        meta_resp = _mock_response(200, make_file_item("doc.pdf"))
        content_resp = _mock_response(200, content=b"PDF-content")
        mock_requests.side_effect = [meta_resp, content_resp]

        client = OneDriveClient(sample_config)
        metadata, content = client.download("item-123")
        assert content == b"PDF-content"
        assert metadata["name"] == "doc.pdf"

    def test_download_stream(self, mock_requests, sample_config):
        meta_resp = _mock_response(200, make_file_item("big.zip"))
        content_resp = _mock_response(200, content=b"big-file-content")
        mock_requests.side_effect = [meta_resp, content_resp]

        client = OneDriveClient(sample_config)
        metadata, stream = client.download_stream("item-123")
        assert stream.read() == b"big-file-content"


class TestClientUpload:
    def test_simple_upload(self, mock_requests, sample_config):
        mock_requests.return_value = _mock_response(201, make_upload_response("file.txt"))

        client = OneDriveClient(sample_config)
        meta = client.upload(b"hello", "file.txt")
        assert meta["name"] == "file.txt"

    def test_chunked_upload(self, mock_requests, sample_config):
        # Session creation response
        session_resp = _mock_response(200, {"uploadUrl": "https://upload.example.com/session"})
        # Final chunk response
        final_resp = _mock_response(201, make_upload_response("big.bin"))

        mock_requests.return_value = session_resp

        client = OneDriveClient(sample_config)
        data = b"x" * 100
        stream = io.BytesIO(data)

        with patch("aisquare_pipe_onedrive.client.requests.put") as mock_put:
            mock_put.return_value = _mock_response(201, make_upload_response("big.bin"))
            meta = client.upload_chunked(stream, "big.bin", len(data))
            assert meta["name"] == "big.bin"

    def test_should_chunk(self, sample_config):
        client = OneDriveClient(sample_config)
        assert client.should_chunk(5 * 1024 * 1024) is True  # 5MB > 4MB
        assert client.should_chunk(3 * 1024 * 1024) is False  # 3MB < 4MB


class TestClientErrorMapping:
    def test_api_error_raises_pipeline_error(self, mock_requests, sample_config):
        mock_requests.return_value = _mock_response(404, {"error": {"code": "itemNotFound"}})
        client = OneDriveClient(sample_config)
        with pytest.raises(PipelineError, match="404"):
            client.download("nonexistent")

    def test_auth_error_raises_config_error(self, mock_requests, sample_config):
        mock_requests.return_value = _mock_response(401, {"error": {"code": "InvalidAuthenticationToken"}})
        client = OneDriveClient(sample_config)
        with pytest.raises(ConfigValidationError, match="401"):
            client.validate()


class TestClientRetry:
    def test_retry_on_429(self, mock_requests, sample_config):
        throttle_resp = _mock_response(429, headers={"Retry-After": "0"})
        ok_resp = _mock_response(200, {"id": "drive-123"})
        mock_requests.side_effect = [throttle_resp, ok_resp]

        client = OneDriveClient(sample_config)
        with patch("aisquare_pipe_onedrive.client.time.sleep"):
            assert client.validate() is True
        assert mock_requests.call_count == 2

    def test_retry_on_503(self, mock_requests, sample_config):
        err_resp = _mock_response(503)
        ok_resp = _mock_response(200, {"id": "drive-123"})
        mock_requests.side_effect = [err_resp, ok_resp]

        client = OneDriveClient(sample_config)
        with patch("aisquare_pipe_onedrive.client.time.sleep"):
            assert client.validate() is True
