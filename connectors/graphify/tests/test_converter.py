"""GraphifyConverter — 1:1 shape and metadata carry-forward."""

from __future__ import annotations

from aisquare.pipe import DataEnvelope

from aisquare_pipe_graphify.constants import CHECKOUT_CONTENT_TYPE, GRAPH_CONTENT_TYPE
from aisquare_pipe_graphify.converter import GraphifyConverter


def _checkout_envelope(checkout: str) -> DataEnvelope:
    return DataEnvelope(
        content_type=CHECKOUT_CONTENT_TYPE,
        data={"path": checkout, "head_sha": "a" * 40},
        source_id="github:owner/name@aaaaaaaaaaaa",
        metadata={
            "repo_full_name": "owner/name",
            "head_sha": "a" * 40,
            "default_branch": "main",
            "description": "A repo",
        },
    )


class TestConvert:
    def test_types_and_one_to_one_shape(self, stub, checkout):
        converter = GraphifyConverter(graphify_bin=str(stub.bin), preflight=False)
        assert converter.from_type == CHECKOUT_CONTENT_TYPE
        assert converter.to_type == GRAPH_CONTENT_TYPE
        out = converter.convert(_checkout_envelope(checkout))
        assert out.content_type == GRAPH_CONTENT_TYPE
        assert out.data["report_md"].startswith("# Graph report")
        assert out.data["stats"] == {"nodes": 12, "edges": 34, "communities": 3}

    def test_source_metadata_carries_forward(self, stub, checkout):
        # Dropping metadata loses sink-required keys (head_sha, repo_full_name).
        converter = GraphifyConverter(graphify_bin=str(stub.bin), preflight=False)
        out = converter.convert(_checkout_envelope(checkout))
        assert out.metadata["head_sha"] == "a" * 40
        assert out.metadata["repo_full_name"] == "owner/name"
        assert out.metadata["description"] == "A repo"
        assert out.source_id == "github:owner/name@aaaaaaaaaaaa"

    def test_converter_adds_graph_metadata(self, stub, checkout):
        converter = GraphifyConverter(graphify_bin=str(stub.bin), preflight=False)
        out = converter.convert(_checkout_envelope(checkout))
        assert out.metadata["node_count"] == 12
        assert out.metadata["edge_count"] == 34
        assert out.metadata["community_count"] == 3
        assert out.metadata["token_count"] > 0
        assert out.metadata["tier"] == "ast"
        assert "enrichment_error" not in out.metadata

    def test_enrichment_error_rides_metadata_on_fallback(self, stub, checkout):
        stub.touch("FAIL_EXTRACT")
        converter = GraphifyConverter(
            backend="claude", api_key="sk-test", graphify_bin=str(stub.bin), preflight=False
        )
        out = converter.convert(_checkout_envelope(checkout))
        assert out.metadata["tier"] == "ast"
        assert "credit balance" in out.metadata["enrichment_error"]
