"""Tests for the LocalClient wrapper — real filesystem tests using tmp_path."""

from __future__ import annotations

import io

import pytest

from aisquare.pipe.errors import ConfigValidationError, PipelineError

from aisquare_pipe_local.client import LocalClient

from tests.helpers import create_test_file, create_test_tree


class TestClientInit:
    def test_init_with_valid_path(self, tmp_path):
        client = LocalClient({"base_path": str(tmp_path)})
        assert client._base_path == tmp_path.resolve()

    def test_init_missing_base_path(self):
        with pytest.raises(ConfigValidationError, match="base_path"):
            LocalClient({})

    def test_init_empty_base_path(self):
        with pytest.raises(ConfigValidationError, match="base_path"):
            LocalClient({"base_path": ""})


class TestClientValidate:
    def test_valid_directory(self, tmp_path):
        client = LocalClient({"base_path": str(tmp_path)})
        assert client.validate() is True

    def test_nonexistent_directory(self, tmp_path):
        client = LocalClient({"base_path": str(tmp_path / "nonexistent")})
        with pytest.raises(ConfigValidationError, match="does not exist"):
            client.validate()

    def test_file_not_directory(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("hello")
        client = LocalClient({"base_path": str(f)})
        with pytest.raises(ConfigValidationError, match="not a directory"):
            client.validate()

    def test_writable_check(self, tmp_path):
        client = LocalClient({"base_path": str(tmp_path)})
        assert client.validate(writable=True) is True


class TestClientListFiles:
    def test_single_level(self, populated_config):
        client = LocalClient(populated_config)
        files = list(client.list_files())
        names = [f.name for f in files]
        assert "a.txt" in names
        assert "b.pdf" in names
        # Should not include subdirectories as files
        assert "sub" not in names
        assert "empty_dir" not in names

    def test_recursive(self, populated_config):
        client = LocalClient(populated_config)
        files = list(client.list_files(recursive=True))
        names = [f.name for f in files]
        assert "a.txt" in names
        assert "c.txt" in names
        assert "d.csv" in names
        assert len(names) == 4

    def test_subdirectory(self, populated_config):
        client = LocalClient(populated_config)
        files = list(client.list_files("sub"))
        names = [f.name for f in files]
        assert names == ["c.txt"]

    def test_empty_directory(self, tmp_path):
        client = LocalClient({"base_path": str(tmp_path)})
        files = list(client.list_files())
        assert files == []


class TestClientReadFile:
    def test_reads_correct_content(self, populated_config):
        client = LocalClient(populated_config)
        data = client.read_file("a.txt")
        assert data == b"content-a"

    def test_reads_nested(self, populated_config):
        client = LocalClient(populated_config)
        data = client.read_file("sub/c.txt")
        assert data == b"content-c"

    def test_file_not_found(self, sample_config):
        client = LocalClient(sample_config)
        with pytest.raises(PipelineError, match="not found"):
            client.read_file("nonexistent.txt")


class TestClientReadStream:
    def test_returns_readable_stream(self, populated_config):
        client = LocalClient(populated_config)
        stream = client.read_stream("a.txt")
        try:
            assert stream.read() == b"content-a"
        finally:
            stream.close()


class TestClientWriteFile:
    def test_basic_write(self, sample_config, tmp_path):
        client = LocalClient(sample_config)
        meta = client.write_file(b"hello world", "output.txt")
        assert (tmp_path / "output.txt").read_bytes() == b"hello world"
        assert meta["size"] == 11

    def test_creates_parent_dirs(self, sample_config, tmp_path):
        client = LocalClient(sample_config)
        client.write_file(b"nested", "a/b/c.txt")
        assert (tmp_path / "a" / "b" / "c.txt").read_bytes() == b"nested"

    def test_conflict_fail(self, sample_config, tmp_path):
        client = LocalClient(sample_config)
        client.write_file(b"first", "file.txt")
        with pytest.raises(PipelineError, match="already exists"):
            client.write_file(b"second", "file.txt", conflict="fail")

    def test_conflict_overwrite(self, sample_config, tmp_path):
        client = LocalClient(sample_config)
        client.write_file(b"first", "file.txt")
        client.write_file(b"second", "file.txt", conflict="overwrite")
        assert (tmp_path / "file.txt").read_bytes() == b"second"

    def test_conflict_rename(self, sample_config, tmp_path):
        client = LocalClient(sample_config)
        client.write_file(b"first", "file.txt")
        meta = client.write_file(b"second", "file.txt", conflict="rename")
        # Original still exists
        assert (tmp_path / "file.txt").read_bytes() == b"first"
        # Renamed file was created
        assert (tmp_path / "file_1.txt").read_bytes() == b"second"
        assert "file_1.txt" in meta["path"]

    def test_conflict_rename_increments(self, sample_config, tmp_path):
        client = LocalClient(sample_config)
        client.write_file(b"v1", "f.txt")
        client.write_file(b"v2", "f.txt", conflict="rename")
        client.write_file(b"v3", "f.txt", conflict="rename")
        assert (tmp_path / "f_1.txt").exists()
        assert (tmp_path / "f_2.txt").exists()


class TestClientWriteStream:
    def test_writes_from_stream(self, sample_config, tmp_path):
        client = LocalClient(sample_config)
        stream = io.BytesIO(b"stream-data")
        meta = client.write_stream(stream, "streamed.bin")
        assert (tmp_path / "streamed.bin").read_bytes() == b"stream-data"
        assert meta["size"] == len(b"stream-data")


class TestClientPathTraversal:
    def test_traversal_blocked(self, sample_config):
        client = LocalClient(sample_config)
        with pytest.raises(PipelineError, match="outside base_path"):
            client.read_file("../../etc/passwd")

    def test_traversal_blocked_on_write(self, sample_config):
        client = LocalClient(sample_config)
        with pytest.raises(PipelineError, match="outside base_path"):
            client.write_file(b"evil", "../escape.txt")
