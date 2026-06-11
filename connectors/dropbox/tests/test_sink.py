"""Tests for DropboxSink connector."""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest

from aisquare.pipe.core.envelope import DataEnvelope, PushParams

from aisquare_pipe_dropbox.connector import DropboxSink

from tests.helpers import make_file_metadata


@pytest.fixture
def mock_client():
    """Patch DropboxClient and return the mock instance."""
    with patch("aisquare_pipe_dropbox.connector.DropboxClient") as mock_cls:
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


class TestDropboxSinkPush:
    def test_push_bytes(self, mock_client):
        result_meta = make_file_metadata("test.txt", "/test.txt", file_id="id:123")
        mock_client.upload.return_value = result_meta
        mock_client.should_chunk.return_value = False

        sink = DropboxSink()
        envelope = _make_envelope(data=b"hello")
        result = sink.push(envelope, {"access_token": "tok"})

        assert result.success is True
        assert result.ref == "id:123"
        assert result.metadata["path"] == "/test.txt"
        mock_client.upload.assert_called_once()

    def test_push_string(self, mock_client):
        result_meta = make_file_metadata("note.txt")
        mock_client.upload.return_value = result_meta

        sink = DropboxSink()
        envelope = _make_envelope(data="text content", filename="note.txt")
        result = sink.push(envelope, {"access_token": "tok"})

        assert result.success is True
        # Verify it was encoded to bytes
        call_args = mock_client.upload.call_args
        assert call_args[0][0] == b"text content"

    def test_push_dict(self, mock_client):
        result_meta = make_file_metadata("data.json")
        mock_client.upload.return_value = result_meta

        sink = DropboxSink()
        envelope = _make_envelope(data={"key": "value"}, filename="data.json")
        result = sink.push(envelope, {"access_token": "tok"})

        assert result.success is True
        call_args = mock_client.upload.call_args
        assert b'"key"' in call_args[0][0]

    def test_push_stream(self, mock_client):
        result_meta = make_file_metadata("big.bin")
        mock_client.upload_chunked.return_value = result_meta

        sink = DropboxSink()
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
        result_meta = make_file_metadata("large.bin")
        mock_client.should_chunk.return_value = True
        mock_client.upload_chunked.return_value = result_meta

        sink = DropboxSink()
        envelope = _make_envelope(data=b"x" * 1000, filename="large.bin")
        result = sink.push(envelope, {"access_token": "tok"})

        assert result.success is True
        mock_client.upload_chunked.assert_called_once()

    def test_push_target_path_from_params(self, mock_client):
        result_meta = make_file_metadata("file.txt")
        mock_client.upload.return_value = result_meta
        mock_client.should_chunk.return_value = False

        sink = DropboxSink()
        envelope = _make_envelope(data=b"data", filename="file.txt")
        params = PushParams(params={"target_path": "/reports/2025"})
        sink.push(envelope, {"access_token": "tok"}, params)

        call_args = mock_client.upload.call_args
        assert call_args[0][1] == "/reports/2025/file.txt"

    def test_push_overwrite_mode(self, mock_client):
        import dropbox.files

        result_meta = make_file_metadata("file.txt")
        mock_client.upload.return_value = result_meta
        mock_client.should_chunk.return_value = False

        sink = DropboxSink()
        envelope = _make_envelope(data=b"data")
        params = PushParams(params={"overwrite": True})
        sink.push(envelope, {"access_token": "tok"}, params)

        call_args = mock_client.upload.call_args
        assert call_args[0][2] == dropbox.files.WriteMode.overwrite

    def test_push_default_filename(self, mock_client):
        result_meta = make_file_metadata("unnamed_file")
        mock_client.upload.return_value = result_meta
        mock_client.should_chunk.return_value = False

        sink = DropboxSink()
        envelope = DataEnvelope(
            content_type="text/plain",
            data=b"data",
            source_id="test",
            metadata={},  # no filename
        )
        sink.push(envelope, {"access_token": "tok"})

        call_args = mock_client.upload.call_args
        assert "unnamed_file" in call_args[0][1]

    def test_push_failure_returns_error(self, mock_client):
        mock_client.upload.side_effect = Exception("Upload failed: network error")
        mock_client.should_chunk.return_value = False

        sink = DropboxSink()
        envelope = _make_envelope(data=b"data")
        result = sink.push(envelope, {"access_token": "tok"})

        assert result.success is False
        assert "network error" in result.error


class TestDropboxSinkValidateConfig:
    def test_valid(self, mock_client):
        mock_client.validate.return_value = True
        sink = DropboxSink()
        assert sink.validate_config({"access_token": "tok"}) is True

    def test_missing_keys(self):
        sink = DropboxSink()
        assert sink.validate_config({}) is False


class TestDropboxSinkAttributes:
    def test_accepts_all_types(self):
        sink = DropboxSink()
        envelope = DataEnvelope(
            content_type="video/mp4", data=b"video", source_id="test"
        )
        assert sink.accepts(envelope) is True

    def test_max_size(self):
        sink = DropboxSink()
        assert sink.max_size() == 350 * 1024 * 1024 * 1024
