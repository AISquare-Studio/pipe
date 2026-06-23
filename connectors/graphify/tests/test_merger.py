"""GraphMerger — prefix isolation, DiGraph collapse, lib dedup, bridges."""

from __future__ import annotations

import json

import pytest

# Both faces need the [engine] extra — skip cleanly without it (CI validator
# runs connectors bare).
q = pytest.importorskip(
    "aisquare_pipe_graphify.query", reason="[engine] extra not installed"
)
merge_graphs = pytest.importorskip(
    "aisquare_pipe_graphify.merger", reason="[engine] extra not installed"
).merge_graphs

# Two repos that BOTH define a node id "config" (collision bait), both import
# the external lib "stripe" (no source_file → dedup bait), and the web repo
# calls the api repo's subscribe view (the bridge the extractors would find).
_WEB = {
    "directed": True,
    "nodes": [
        {"id": "subscribebutton", "label": "SubscribeButton",
         "source_file": "src/Subscribe.tsx", "community": 1},
        {"id": "config", "label": "config", "source_file": "src/config.ts"},
        {"id": "stripe", "label": "stripe"},  # external lib — no source_file
    ],
    "links": [
        {"source": "subscribebutton", "target": "config", "relation": "imports"},
        {"source": "subscribebutton", "target": "config", "relation": "references"},
        {"source": "subscribebutton", "target": "stripe", "relation": "imports"},
    ],
}
_API = {
    "directed": True,
    "nodes": [
        {"id": "subscribeview", "label": "SubscribeView",
         "source_file": "api/views/subscribe.py", "community": 2},
        {"id": "config", "label": "config", "source_file": "api/config.py"},
        {"id": "stripe", "label": "stripe"},  # same external lib
    ],
    "links": [
        {"source": "subscribeview", "target": "stripe", "relation": "calls"},
        {"source": "subscribeview", "target": "config", "relation": "imports"},
    ],
}

_BRIDGE = {
    "kind": "http_call",
    "source_repo": "acme/web",
    "target_repo": "acme/api",
    "source_file": "src/Subscribe.tsx",
    "target_file": "api/views/subscribe.py",
    "evidence": "fetch('/api/v2/subscribe') matches route subscribe.py",
    "confidence": 0.8,
}


def _merge(bridges=None):
    return merge_graphs(
        [("acme/web", json.dumps(_WEB)), ("acme/api", json.dumps(_API))],
        bridge_edges=bridges,
    )


class TestCompose:
    def test_same_named_nodes_stay_isolated_via_repo_prefix(self):
        merged_json, stats = _merge()
        G = q.load_graph_from_json(merged_json)
        assert "acme/web::config" in G
        assert "acme/api::config" in G
        assert G.nodes["acme/web::config"]["repo"] == "acme/web"
        assert stats["repos"] == 2

    def test_direction_is_preserved(self):
        merged_json, _ = _merge()
        G = q.load_graph_from_json(merged_json)
        assert G.is_directed()
        assert G.has_edge("acme/web::subscribebutton", "acme/web::config")
        assert not G.has_edge("acme/web::config", "acme/web::subscribebutton")

    def test_parallel_edges_collapse_to_one_with_relations_list(self):
        # D4 middle option: imports + references between the same pair become
        # ONE traversable edge carrying both relation types.
        merged_json, _ = _merge()
        data = json.loads(merged_json)
        edges = [
            e for e in data["links"]
            if e["source"] == "acme/web::subscribebutton" and e["target"] == "acme/web::config"
        ]
        assert len(edges) == 1
        assert sorted(edges[0]["relations"]) == ["imports", "references"]

    def test_external_lib_deduped_across_repos_with_remapped_edges(self):
        merged_json, stats = _merge()
        G = q.load_graph_from_json(merged_json)
        stripe_nodes = [n for n, d in G.nodes(data=True) if d.get("label") == "stripe"]
        assert len(stripe_nodes) == 1
        canonical = stripe_nodes[0]
        # BOTH repos' edges now land on the one canonical lib node.
        assert G.has_edge("acme/web::subscribebutton", canonical)
        assert G.has_edge("acme/api::subscribeview", canonical)
        assert stats["external_nodes_deduped"] == 1

    def test_communities_pass_through_untouched_no_leiden_rerun(self):
        merged_json, _ = _merge()
        G = q.load_graph_from_json(merged_json)
        assert G.nodes["acme/web::subscribebutton"]["community"] == 1
        assert G.nodes["acme/api::subscribeview"]["community"] == 2


class TestBridges:
    def test_exact_file_mapping_draws_the_cross_repo_edge(self):
        merged_json, stats = _merge(bridges=[_BRIDGE])
        G = q.load_graph_from_json(merged_json)
        assert G.has_edge("acme/web::subscribebutton", "acme/api::subscribeview")
        edge = G["acme/web::subscribebutton"]["acme/api::subscribeview"]
        assert edge["bridge"] is True
        assert edge["kind"] == "http_call"
        assert edge["confidence"] == 0.8
        assert stats["bridges"][0]["mapping"] == "exact"

    def test_unmapped_file_anchors_on_synthetic_repo_root(self):
        bad = dict(_BRIDGE, target_file="api/views/GONE.py")
        merged_json, stats = _merge(bridges=[bad])
        G = q.load_graph_from_json(merged_json)
        assert "acme/api::__repo__" in G
        assert G.has_edge("acme/web::subscribebutton", "acme/api::__repo__")
        assert stats["bridges"][0]["mapping"] == "target_root"

    def test_cross_repo_path_traverses_the_bridge_end_to_end(self):
        # The masternode payoff: one traversal walks FE → bridge → BE.
        merged_json, _ = _merge(bridges=[_BRIDGE])
        G = q.load_graph_from_json(merged_json)
        text = q.shortest_path_text(G, "SubscribeButton", "SubscribeView")
        assert "1 hops" in text or "http_call" in text

    def test_blast_radius_crosses_the_bridge_with_bridge_relations(self):
        # Changing the BE view impacts the FE component THROUGH the bridge —
        # but only when the bridge relation kinds are followed: the engine's
        # default dependency set is code-level only. extra_relations is the
        # seam merged-graph consumers use (the PR-review 🔴 fix).
        from aisquare_pipe_graphify.merger import BRIDGE_RELATIONS

        merged_json, _ = _merge(bridges=[_BRIDGE])
        G = q.load_graph_from_json(merged_json)
        without = q.blast_radius_text(G, "SubscribeView", depth=2)
        assert "SubscribeButton" not in without  # defaults stop at the repo boundary
        crossed = q.blast_radius_text(
            G, "SubscribeView", depth=2, extra_relations=BRIDGE_RELATIONS
        )
        assert "SubscribeButton" in crossed
        assert "[http_call]" in crossed  # via-relation names the bridge
