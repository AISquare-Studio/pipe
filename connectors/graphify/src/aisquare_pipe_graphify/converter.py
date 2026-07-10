"""GraphifyConverter — the Python-API composition face (checkout → graph bundle)."""

from __future__ import annotations

import base64

from aisquare.pipe import DataEnvelope, TypeConverter

from aisquare_pipe_graphify.constants import CHECKOUT_CONTENT_TYPE, GRAPH_CONTENT_TYPE
from aisquare_pipe_graphify.engine import GraphifyEngine


class GraphifyConverter(TypeConverter):
    """checkout handle -> graph bundle. 1 envelope in, 1 envelope out.

    No framework config channel exists for converters — ALL knobs are
    constructor args, armed by whoever builds the Pipeline. The consuming sink
    MUST declare GRAPH_CONTENT_TYPE exactly (never a wildcard), or the
    EXACT/WILDCARD match short-circuits and this converter silently never runs.

    ``prior_state_tar`` (V2 Phase 3): the previous build's captured
    graphify-out state — restored into the checkout before the engine runs so
    its incremental/content-hash-cache paths activate. Per-build, which is
    fine: callers construct one converter per pipeline run.
    """

    from_type = CHECKOUT_CONTENT_TYPE
    to_type = GRAPH_CONTENT_TYPE

    def __init__(self, backend=None, api_key=None, prior_state_tar=None, **engine_kwargs) -> None:
        self._engine = GraphifyEngine(backend=backend, api_key=api_key, **engine_kwargs)
        self._prior_state_tar = prior_state_tar

    def convert(self, envelope: DataEnvelope) -> DataEnvelope:
        art = self._engine.build(envelope.data["path"], prior_state=self._prior_state_tar)
        meta = dict(envelope.metadata)  # carry source metadata forward — dropping it
        meta.update(  # loses sink-required keys like head_sha/repo_full_name
            node_count=art.nodes,
            edge_count=art.edges,
            community_count=art.communities,
            token_count=art.token_count,
            tier=art.tier,
            graphify_version=art.graphify_version,
            llm_tokens_in=art.llm_tokens_in,
            llm_tokens_out=art.llm_tokens_out,
            llm_cost_estimate=art.llm_cost_estimate,
        )
        if art.enrichment_error:
            meta["enrichment_error"] = art.enrichment_error
        data = {
            "report_md": art.report_md,
            "graph_json": art.graph_json,
            "stats": {
                "nodes": art.nodes,
                "edges": art.edges,
                "communities": art.communities,
            },
        }
        if art.state_tar:
            # b64 keeps the envelope JSON-safe; the sink persists it as the
            # next build's prior_state (the incremental round-trip).
            data["state_tar_b64"] = base64.b64encode(art.state_tar).decode("ascii")
        return DataEnvelope(
            content_type=GRAPH_CONTENT_TYPE,
            data=data,
            source_id=envelope.source_id,
            metadata=meta,
        )
