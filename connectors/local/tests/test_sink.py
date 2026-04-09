"""Tests for LocalSink connector."""

from __future__ import annotations

import io
import json

import pytest

from aisquare.pipe.core.envelope import DataEnvelope, PushParams

from aisquare_pipe_local.connector import LocalSink


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


class TestLocalSinkPush:
    def test_push_bytes(self, sample_config, tmp_path):
        sink = LocalSink()
        envelope = _make_envelope(data=b"hello")
        result = sink.push(envelope, sample_config)

        assert result.success is True
        assert (tmp_path / "test.txt").read_bytes() == b"hello"

    def test_push_string(self, sample_config, tmp_path):
        sink = LocalSink()
        envelope = _make_envelope(data="text content", filename="note.txt")
        result = sink.push(envelope, sample_config)

        assert result.success is True
        assert (tmp_path / "note.txt").read_bytes() == b"text content"

    def test_push_dict(self, sample_config, tmp_path):
        sink = LocalSink()
        envelope = _make_envelope(data={"key": "value"}, filename="data.json")
        result = sink.push(envelope, sample_config)

        assert result.success is True
        written = json.loads((tmp_path / "data.json").read_bytes())
        assert written == {"key": "value"}

    def test_push_stream(self, sample_config, tmp_path):
        sink = LocalSink()
        stream = io.BytesIO(b"stream-data")
        envelope = DataEnvelope(
            content_type="application/octet-stream",
            data=b"",
            source_id="test",
            stream=stream,
            metadata={"filename": "streamed.bin"},
        )
        result = sink.push(envelope, sample_config)

        assert result.success is True
        assert (tmp_path / "streamed.bin").read_bytes() == b"stream-data"

    def test_push_target_path(self, sample_config, tmp_path):
        sink = LocalSink()
        envelope = _make_envelope(data=b"data", filename="file.txt")
        params = PushParams(params={"target_path": "reports/2025"})
        result = sink.push(envelope, sample_config, params)

        assert result.success is True
        assert (tmp_path / "reports" / "2025" / "file.txt").read_bytes() == b"data"

    def test_push_preserves_directory_structure(self, sample_config, tmp_path):
        """With preserve_paths=True (default), metadata['path'] recreates subdirs."""
        sink = LocalSink()
        envelope = DataEnvelope(
            content_type="text/plain",
            data=b"nested-content",
            source_id="test",
            metadata={"filename": "c.txt", "path": "sub/deep/c.txt"},
        )
        result = sink.push(envelope, sample_config)

        assert result.success is True
        assert (tmp_path / "sub" / "deep" / "c.txt").read_bytes() == b"nested-content"

    def test_push_preserves_cloud_paths(self, sample_config, tmp_path):
        """Cloud sources use absolute paths like /Documents/file.pdf — leading slash is stripped."""
        sink = LocalSink()
        envelope = DataEnvelope(
            content_type="text/plain",
            data=b"cloud-data",
            source_id="dropbox-source",
            metadata={"filename": "report.pdf", "path": "/Documents/Reports/report.pdf"},
        )
        result = sink.push(envelope, sample_config)

        assert result.success is True
        assert (tmp_path / "Documents" / "Reports" / "report.pdf").read_bytes() == b"cloud-data"

    def test_push_flatten_with_preserve_false(self, sample_config, tmp_path):
        """With preserve_paths=False, only filename is used — no subdirs."""
        sink = LocalSink()
        envelope = DataEnvelope(
            content_type="text/plain",
            data=b"flat",
            source_id="test",
            metadata={"filename": "c.txt", "path": "sub/deep/c.txt"},
        )
        params = PushParams(params={"preserve_paths": False})
        result = sink.push(envelope, sample_config, params)

        assert result.success is True
        assert (tmp_path / "c.txt").read_bytes() == b"flat"
        assert not (tmp_path / "sub").exists()

    def test_push_conflict_fail(self, sample_config, tmp_path):
        sink = LocalSink()
        envelope = _make_envelope(data=b"first")
        sink.push(envelope, sample_config)

        result = sink.push(envelope, sample_config)
        assert result.success is False
        assert "already exists" in result.error

    def test_push_conflict_overwrite(self, sample_config, tmp_path):
        sink = LocalSink()
        envelope = _make_envelope(data=b"first")
        sink.push(envelope, sample_config)

        envelope2 = _make_envelope(data=b"second")
        params = PushParams(params={"conflict": "overwrite"})
        result = sink.push(envelope2, sample_config, params)

        assert result.success is True
        assert (tmp_path / "test.txt").read_bytes() == b"second"

    def test_push_conflict_rename(self, sample_config, tmp_path):
        sink = LocalSink()
        envelope = _make_envelope(data=b"first")
        sink.push(envelope, sample_config)

        envelope2 = _make_envelope(data=b"second")
        params = PushParams(params={"conflict": "rename"})
        result = sink.push(envelope2, sample_config, params)

        assert result.success is True
        assert (tmp_path / "test.txt").read_bytes() == b"first"
        assert (tmp_path / "test_1.txt").read_bytes() == b"second"

    def test_push_default_filename(self, sample_config, tmp_path):
        sink = LocalSink()
        envelope = DataEnvelope(
            content_type="text/plain",
            data=b"data",
            source_id="test",
            metadata={},
        )
        result = sink.push(envelope, sample_config)

        assert result.success is True
        assert (tmp_path / "unnamed_file").exists()

    def test_push_failure_returns_error(self):
        sink = LocalSink()
        envelope = _make_envelope(data=b"data")
        result = sink.push(envelope, {"base_path": "/nonexistent/path/xyz"})

        assert result.success is False
        assert result.error is not None


class TestLocalSinkValidateConfig:
    def test_valid(self, sample_config):
        sink = LocalSink()
        assert sink.validate_config(sample_config) is True

    def test_missing_keys(self):
        sink = LocalSink()
        assert sink.validate_config({}) is False


class TestLocalSinkAttributes:
    def test_accepts_all_types(self):
        sink = LocalSink()
        envelope = DataEnvelope(
            content_type="video/mp4", data=b"video", source_id="test"
        )
        assert sink.accepts(envelope) is True

    def test_max_size(self):
        sink = LocalSink()
        assert sink.max_size() is None
