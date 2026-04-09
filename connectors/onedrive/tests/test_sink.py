"""Tests for OneDriveSink connector."""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest

from aisquare.pipe.core.envelope import DataEnvelope, PushParams

from aisquare_pipe_onedrive.connector import OneDriveSink

from tests.helpers import make_upload_response


@pytest.fixture
def mock_client():
    """Patch OneDriveClient and return the mock instance."""
    with patch("aisquare_pipe_onedrive.connector.OneDriveClient") as mock_cls:
        mock_instance = MagicMock()
        mock_cls.return_value = mock_instance
        yield mock_instance


def _make_envelope(
    data=b"hello world",
    content_type="text/plain",
    filename="test.txt",
    **meta,
):
    return DataEnvelope(
        content_type=content_type,
        data=data,
        source_id="test",
        metadata={"filename": filename, **meta},
    )


class TestOneDriveSinkPush:
    def test_push_bytes(self, mock_client):
        mock_client.upload.return_value = make_upload_response("test.txt", "id-123")
        mock_client.should_chunk.return_value = False

        sink = OneDriveSink()
        envelope = _make_envelope(data=b"hello")
        result = sink.push(envelope, {"access_token": "tok"})

        assert result.success is True
        assert result.ref == "id-123"
        mock_client.upload.assert_called_once()

    def test_push_string(self, mock_client):
        mock_client.upload.return_value = make_upload_response("note.txt")
        mock_client.should_chunk.return_value = False

        sink = OneDriveSink()
        envelope = _make_envelope(data="text content", filename="note.txt")
        result = sink.push(envelope, {"access_token": "tok"})

        assert result.success is True
        call_args = mock_client.upload.call_args
        assert call_args[0][0] == b"text content"

    def test_push_dict(self, mock_client):
        mock_client.upload.return_value = make_upload_response("data.json")

        sink = OneDriveSink()
        envelope = _make_envelope(data={"key": "value"}, filename="data.json")
        result = sink.push(envelope, {"access_token": "tok"})

        assert result.success is True
        call_args = mock_client.upload.call_args
        assert b'"key"' in call_args[0][0]

    def test_push_stream(self, mock_client):
        mock_client.upload_chunked.return_value = make_upload_response("big.bin")

        sink = OneDriveSink()
        stream = io.BytesIO(b"stream-data")
        envelope = DataEnvelope(
            content_type="application/octet-stream",
            data=b"",
            source_id="test",
            stream=stream,
            metadata={"filename": "big.bin", "size": 1000},
        )
        result = sink.push(envelope, {"access_token": "tok"})

        assert result.success is True
        mock_client.upload_chunked.assert_called_once()

    def test_push_large_bytes_uses_chunked(self, mock_client):
        mock_client.should_chunk.return_value = True
        mock_client.upload_chunked.return_value = make_upload_response("large.bin")

        sink = OneDriveSink()
        envelope = _make_envelope(data=b"x" * 1000, filename="large.bin")
        result = sink.push(envelope, {"access_token": "tok"})

        assert result.success is True
        mock_client.upload_chunked.assert_called_once()

    def test_push_target_path_from_params(self, mock_client):
        mock_client.upload.return_value = make_upload_response("file.txt")
        mock_client.should_chunk.return_value = False

        sink = OneDriveSink()
        envelope = _make_envelope(data=b"data", filename="file.txt")
        params = PushParams(params={"target_path": "/Reports/2025"})
        sink.push(envelope, {"access_token": "tok"}, params)

        call_args = mock_client.upload.call_args
        assert call_args[0][1] == "Reports/2025/file.txt"

    def test_push_conflict_replace(self, mock_client):
        mock_client.upload.return_value = make_upload_response("file.txt")
        mock_client.should_chunk.return_value = False

        sink = OneDriveSink()
        envelope = _make_envelope(data=b"data")
        params = PushParams(params={"conflict": "replace"})
        sink.push(envelope, {"access_token": "tok"}, params)

        call_args = mock_client.upload.call_args
        assert call_args[0][2] == "replace"

    def test_push_default_filename(self, mock_client):
        mock_client.upload.return_value = make_upload_response("unnamed_file")
        mock_client.should_chunk.return_value = False

        sink = OneDriveSink()
        envelope = DataEnvelope(
            content_type="text/plain",
            data=b"data",
            source_id="test",
            metadata={},
        )
        sink.push(envelope, {"access_token": "tok"})

        call_args = mock_client.upload.call_args
        assert "unnamed_file" in call_args[0][1]

    def test_push_failure_returns_error(self, mock_client):
        mock_client.upload.side_effect = Exception("Upload failed: network error")
        mock_client.should_chunk.return_value = False

        sink = OneDriveSink()
        envelope = _make_envelope(data=b"data")
        result = sink.push(envelope, {"access_token": "tok"})

        assert result.success is False
        assert "network error" in result.error


class TestOneDriveSinkValidateConfig:
    def test_valid(self, mock_client):
        mock_client.validate.return_value = True
        sink = OneDriveSink()
        assert sink.validate_config({"access_token": "tok"}) is True

    def test_missing_keys(self):
        sink = OneDriveSink()
        assert sink.validate_config({}) is False


class TestOneDriveSinkAttributes:
    def test_accepts_all_types(self):
        sink = OneDriveSink()
        envelope = DataEnvelope(
            content_type="video/mp4", data=b"video", source_id="test"
        )
        assert sink.accepts(envelope) is True

    def test_max_size(self):
        sink = OneDriveSink()
        assert sink.max_size() == 250 * 1024 * 1024 * 1024
