"""Unit tests for file helpers: downloaded-file detection and upload
materialisation."""

from __future__ import annotations

import io

import pytest

from aisquare.pipe.core.envelope import DataEnvelope

from aisquare_pipe_composio.files import (
    find_downloaded_files,
    guess_content_type,
    materialize_upload_file,
)


def _envelope(data, metadata=None, stream=None):
    return DataEnvelope(
        content_type="application/octet-stream",
        data=data,
        source_id="test",
        metadata=metadata or {},
        stream=stream,
    )


class TestFindDownloadedFiles:
    def test_finds_nested_paths(self, tmp_path):
        f1 = tmp_path / "gmail" / "TOOL" / "a.pdf"
        f1.parent.mkdir(parents=True)
        f1.write_bytes(b"a")
        f2 = tmp_path / "gmail" / "TOOL" / "b.png"
        f2.write_bytes(b"b")

        data = {
            "attachments": [{"file": str(f1)}, {"file": str(f2)}],
            "subject": "hi",
        }
        found = find_downloaded_files(data, tmp_path)

        assert {(d.field_path, d.path) for d in found} == {
            ("attachments.0.file", f1),
            ("attachments.1.file", f2),
        }

    def test_ignores_paths_outside_dir(self, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_bytes(b"x")
        data = {"file": str(outside)}
        assert find_downloaded_files(data, tmp_path / "downloads") == []

    def test_ignores_nonexistent_paths(self, tmp_path):
        data = {"file": str(tmp_path / "ghost.bin")}
        assert find_downloaded_files(data, tmp_path) == []

    def test_none_download_dir(self):
        assert find_downloaded_files({"file": "/tmp/x"}, None) == []

    def test_ignores_plain_strings(self, tmp_path):
        data = {"note": "hello", "count": 3, "nested": {"more": ["text"]}}
        assert find_downloaded_files(data, tmp_path) == []


class TestGuessContentType:
    def test_known_extension(self, tmp_path):
        assert guess_content_type(tmp_path / "x.pdf") == "application/pdf"

    def test_unknown_extension(self, tmp_path):
        assert guess_content_type(tmp_path / "x.qqq") == "application/octet-stream"


class TestMaterializeUploadFile:
    def test_bytes_payload(self, tmp_path):
        envelope = _envelope(b"content", metadata={"filename": "doc.txt"})
        path = materialize_upload_file(envelope, tmp_path)
        assert path.parent == tmp_path
        assert path.name.endswith("-doc.txt")
        assert path.read_bytes() == b"content"

    def test_str_payload(self, tmp_path):
        envelope = _envelope("text content")
        path = materialize_upload_file(envelope, tmp_path)
        assert path.read_bytes() == b"text content"
        assert path.name.endswith("-upload.bin")

    def test_stream_payload(self, tmp_path):
        envelope = _envelope(b"", stream=io.BytesIO(b"streamed"))
        path = materialize_upload_file(envelope, tmp_path)
        assert path.read_bytes() == b"streamed"

    def test_filename_is_sanitized(self, tmp_path):
        envelope = _envelope(b"x", metadata={"filename": "../../evil.sh"})
        path = materialize_upload_file(envelope, tmp_path)
        assert path.parent == tmp_path
        assert path.name.endswith("-evil.sh")

    def test_dict_payload_raises(self, tmp_path):
        envelope = _envelope({"not": "a file"})
        with pytest.raises(ValueError, match="Cannot materialize"):
            materialize_upload_file(envelope, tmp_path)
