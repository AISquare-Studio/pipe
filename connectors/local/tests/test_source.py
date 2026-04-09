"""Tests for LocalSource connector."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from pathlib import Path

import pytest

from aisquare.pipe.core.envelope import PullParams

from aisquare_pipe_local.connector import LocalSource

from tests.helpers import create_test_tree


class TestLocalSourcePull:
    def test_yields_envelopes(self, populated_config):
        source = LocalSource()
        envelopes = list(source.pull(populated_config))
        # Top-level only: a.txt, b.pdf
        assert len(envelopes) == 2
        names = {e.metadata["filename"] for e in envelopes}
        assert names == {"a.txt", "b.pdf"}

    def test_envelope_content(self, populated_config):
        source = LocalSource()
        envelopes = list(source.pull(populated_config))
        a_env = next(e for e in envelopes if e.metadata["filename"] == "a.txt")
        assert a_env.data == b"content-a"
        assert a_env.content_type == "text/plain"
        assert a_env.source_id == "local-source"

    def test_recursive(self, populated_config):
        source = LocalSource()
        params = PullParams(params={"recursive": True})
        envelopes = list(source.pull(populated_config, params))
        names = {e.metadata["filename"] for e in envelopes}
        assert names == {"a.txt", "b.pdf", "c.txt", "d.csv"}

    def test_extension_filter(self, populated_config):
        source = LocalSource()
        params = PullParams(params={"recursive": True, "extensions": [".txt"]})
        envelopes = list(source.pull(populated_config, params))
        assert len(envelopes) == 2
        assert all(e.metadata["filename"].endswith(".txt") for e in envelopes)

    def test_limit(self, populated_config):
        source = LocalSource()
        params = PullParams(params={"recursive": True, "limit": 2})
        envelopes = list(source.pull(populated_config, params))
        assert len(envelopes) == 2

    def test_glob_pattern(self, populated_config):
        source = LocalSource()
        params = PullParams(params={"glob": "**/*.csv"})
        envelopes = list(source.pull(populated_config, params))
        assert len(envelopes) == 1
        assert envelopes[0].metadata["filename"] == "d.csv"

    def test_subdirectory_path(self, populated_config):
        source = LocalSource()
        params = PullParams(params={"path": "sub"})
        envelopes = list(source.pull(populated_config, params))
        assert len(envelopes) == 1
        assert envelopes[0].metadata["filename"] == "c.txt"

    def test_large_file_uses_stream(self, tmp_path):
        # Create a file larger than a small threshold
        (tmp_path / "big.bin").write_bytes(b"x" * 1000)
        source = LocalSource()
        params = PullParams(params={"stream_threshold": 500})
        envelopes = list(source.pull({"base_path": str(tmp_path)}, params))
        assert len(envelopes) == 1
        assert envelopes[0].stream is not None
        assert envelopes[0].data == b""
        content = envelopes[0].stream.read()
        envelopes[0].stream.close()
        assert len(content) == 1000

    def test_mime_type_detection(self, populated_config):
        source = LocalSource()
        envelopes = list(source.pull(populated_config))
        pdf_env = next(e for e in envelopes if e.metadata["filename"] == "b.pdf")
        assert pdf_env.content_type == "application/pdf"

    def test_metadata_fields(self, populated_config):
        source = LocalSource()
        envelopes = list(source.pull(populated_config))
        meta = envelopes[0].metadata
        assert "filename" in meta
        assert "path" in meta
        assert "size" in meta
        assert "modified_time" in meta
        assert "created_time" in meta
        assert "permissions" in meta


class TestLocalSourceValidateConfig:
    def test_valid(self, populated_config):
        source = LocalSource()
        assert source.validate_config(populated_config) is True

    def test_missing_base_path(self):
        source = LocalSource()
        assert source.validate_config({}) is False

    def test_nonexistent_directory(self, tmp_path):
        source = LocalSource()
        assert source.validate_config({"base_path": str(tmp_path / "nope")}) is False


class TestLocalSourceListResources:
    def test_returns_resources(self, populated_config):
        source = LocalSource()
        resources = source.list_resources(populated_config)
        names = {r.name for r in resources}
        assert "a.txt" in names
        assert "sub" in names
        assert "empty_dir" in names

    def test_resource_types(self, populated_config):
        source = LocalSource()
        resources = source.list_resources(populated_config)
        folder = next(r for r in resources if r.name == "sub")
        assert folder.resource_type == "folder"
        file_r = next(r for r in resources if r.name == "a.txt")
        assert file_r.resource_type == "file"


class TestLocalSourceAttributes:
    def test_supports_streaming(self):
        assert LocalSource().supports_streaming() is True

    def test_rate_limit(self):
        assert LocalSource().rate_limit() is None
