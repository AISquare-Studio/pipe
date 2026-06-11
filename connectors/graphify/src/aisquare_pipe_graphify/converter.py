"""GraphifyConverter — the Python-API composition face (checkout → graph bundle)."""

from __future__ import annotations

from aisquare.pipe import DataEnvelope, TypeConverter

from aisquare_pipe_graphify.constants import CHECKOUT_CONTENT_TYPE, GRAPH_CONTENT_TYPE
from aisquare_pipe_graphify.engine import GraphifyEngine


class GraphifyConverter(TypeConverter):
    """checkout handle -> graph bundle. 1 envelope in, 1 envelope out.

    No framework config channel exists for converters — ALL knobs are
    constructor args, armed by whoever builds the Pipeline. The consuming sink
    MUST declare GRAPH_CONTENT_TYPE exactly (never a wildcard), or the
    EXACT/WILDCARD match short-circuits and this converter silently never runs.
    """

    from_type = CHECKOUT_CONTENT_TYPE
    to_type = GRAPH_CONTENT_TYPE

    def __init__(self, backend=None, api_key=None, **engine_kwargs) -> None:
        self._engine = GraphifyEngine(backend=backend, api_key=api_key, **engine_kwargs)

    def convert(self, envelope: DataEnvelope) -> DataEnvelope:
        art = self._engine.build(envelope.data["path"])
        meta = dict(envelope.metadata)  # carry source metadata forward — dropping it
        meta.update(  # loses sink-required keys like head_sha/repo_full_name
            node_count=art.nodes,
            edge_count=art.edges,
            community_count=art.communities,
            token_count=art.token_count,
            tier=art.tier,
            graphify_version=art.graphify_version,
        )
        if art.enrichment_error:
            meta["enrichment_error"] = art.enrichment_error
        return DataEnvelope(
            content_type=GRAPH_CONTENT_TYPE,
            data={
                "report_md": art.report_md,
                "graph_json": art.graph_json,
                "stats": {
                    "nodes": art.nodes,
                    "edges": art.edges,
                    "communities": art.communities,
                },
            },
            source_id=envelope.source_id,
            metadata=meta,
        )
