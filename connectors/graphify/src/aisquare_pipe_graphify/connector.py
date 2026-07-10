"""GraphifySource — the public CLI face: graph a LOCAL directory."""

from __future__ import annotations

import logging
from pathlib import Path

from aisquare.pipe import AuthType, DataEnvelope, MetaField, SourceConnector
from aisquare.pipe.errors import ConfigValidationError

from aisquare_pipe_graphify.constants import GRAPH_CONTENT_TYPE
from aisquare_pipe_graphify.engine import GraphifyEngine

logger = logging.getLogger("aisquare.pipe.graphify")


class GraphifySource(SourceConnector):
    """Run graphify on a LOCAL directory and emit the artifacts.

    emit="files" (default): GRAPH_REPORT.md as text/markdown + graph.json as
    application/json, each with metadata filename/path so local-sink writes
    real files. emit="bundle": one GRAPH_CONTENT_TYPE dict envelope for API
    sinks. The AST tier needs no key at all; set backend (+api_key) for the
    LLM-enriched tier.
    """

    name = "graphify"  # config-dict key (entry-point name is graphify-source)
    version = "0.2.0"
    output_types = [GRAPH_CONTENT_TYPE, "text/markdown", "application/json"]
    auth_type = AuthType.NONE
    description = "Knowledge-graph source: graphify a local code tree."

    CONFIG_SPEC = {
        "path": MetaField(type=str, required=True, description="directory to graph (any code tree)"),
        "emit": MetaField(type=str, default="files", description='"files" | "bundle"'),
        "backend": MetaField(
            type=str, description="claude|openai|gemini|deepseek|ollama; unset = free AST tier"
        ),
        "api_key": MetaField(
            type=str, description="LLM key for the chosen backend (not needed for ollama)"
        ),
        "extract_flags": MetaField(type=str, default="--no-viz"),
        "extract_timeout_seconds": MetaField(type=int, default=1500),
        "update_timeout_seconds": MetaField(type=int, default=600),
        "graphify_bin": MetaField(type=str, default="graphify"),
        "preflight": MetaField(type=bool, default=True),
        "fallback_to_ast": MetaField(type=bool, default=True),
    }
    metadata_spec = {
        "node_count": MetaField(type=int),
        "edge_count": MetaField(type=int),
        "community_count": MetaField(type=int),
        "token_count": MetaField(type=int),
        "tier": MetaField(type=str, description='"ast" | "enriched"'),
        "graphify_version": MetaField(type=str),
        "filename": MetaField(type=str, description="set in files mode for filesystem sinks"),
        "path": MetaField(type=str, description="set in files mode for filesystem sinks"),
    }

    def pull(self, config, params=None):
        # Generator body: bad config surfaces on iteration, so compliance's
        # uniterated pull({}) can't explode.
        config = config or {}
        path = config.get("path")
        if not path or not Path(path).is_dir():
            raise ConfigValidationError(
                "graphify config requires 'path' (an existing directory to graph)"
            )
        art = GraphifyEngine.from_config(config).build(path)
        meta = {
            "node_count": art.nodes,
            "edge_count": art.edges,
            "community_count": art.communities,
            "token_count": art.token_count,
            "tier": art.tier,
            "graphify_version": art.graphify_version,
        }
        if art.enrichment_error:
            meta["enrichment_error"] = art.enrichment_error
        sid = f"graphify:{path}"
        if config.get("emit", "files") == "bundle":
            yield DataEnvelope(
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
                source_id=sid,
                metadata=meta,
            )
            return
        yield DataEnvelope(
            content_type="text/markdown",
            data=art.report_md,
            source_id=sid,
            metadata={**meta, "filename": "GRAPH_REPORT.md", "path": "GRAPH_REPORT.md"},
        )
        if art.graph_json:
            yield DataEnvelope(
                content_type="application/json",
                data=art.graph_json,
                source_id=sid,
                metadata={**meta, "filename": "graph.json", "path": "graph.json"},
            )

    def validate_config(self, config):
        try:
            return bool((config or {}).get("path")) and Path(config["path"]).is_dir()
        except Exception:  # noqa: BLE001
            return False
