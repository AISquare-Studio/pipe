"""GraphifySource — files vs bundle emission, config validation."""

from __future__ import annotations

import pytest

from aisquare.pipe.errors import ConfigValidationError
from aisquare_pipe_graphify.connector import GraphifySource
from aisquare_pipe_graphify.constants import GRAPH_CONTENT_TYPE


class TestFilesMode:
    def test_emits_report_and_json_with_filenames_for_local_sink(self, stub, checkout):
        envelopes = list(
            GraphifySource().pull(
                {"path": checkout, "graphify_bin": str(stub.bin), "preflight": False}
            )
        )
        assert [e.content_type for e in envelopes] == ["text/markdown", "application/json"]
        report, graph = envelopes
        assert report.metadata["filename"] == "GRAPH_REPORT.md"
        assert report.data.startswith("# Graph report")
        assert graph.metadata["filename"] == "graph.json"
        assert report.metadata["tier"] == "ast"
        assert report.metadata["node_count"] == 12


class TestBundleMode:
    def test_emits_single_graph_bundle(self, stub, checkout):
        envelopes = list(
            GraphifySource().pull(
                {
                    "path": checkout,
                    "emit": "bundle",
                    "graphify_bin": str(stub.bin),
                    "preflight": False,
                }
            )
        )
        assert len(envelopes) == 1
        env = envelopes[0]
        assert env.content_type == GRAPH_CONTENT_TYPE
        assert env.data["stats"] == {"nodes": 12, "edges": 34, "communities": 3}
        assert env.data["report_md"].startswith("# Graph report")


class TestConfig:
    def test_missing_path_raises_on_iteration(self):
        gen = GraphifySource().pull({})
        with pytest.raises(ConfigValidationError):
            next(gen)

    def test_validate_config_never_raises(self, checkout):
        source = GraphifySource()
        assert source.validate_config({}) is False
        assert source.validate_config({"path": "/definitely/not/here"}) is False
        assert source.validate_config({"path": checkout}) is True
