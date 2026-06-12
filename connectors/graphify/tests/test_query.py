"""query.py — the Python-API query face over a stored graph.json.

Drives the real graphifyy serve/affected functions over an in-memory fixture
graph (requires the [engine] extra in the test venv — no CLI, no stub).
"""

from __future__ import annotations

import json

import pytest

from aisquare_pipe_graphify import query as q

# A miniature web-shop graph: SubscribeButton -> apiClient -> charge -> stripe,
# with a god-ish hub (db) and a second caller of charge for blast-radius.
_FIXTURE = {
    "directed": True,
    "nodes": [
        {"id": "subscribebutton", "label": "SubscribeButton", "kind": "component",
         "source_file": "src/Subscribe.tsx", "source_location": "12"},
        {"id": "apiclient_post", "label": "apiClient.post", "kind": "function",
         "source_file": "src/api.ts"},
        {"id": "charge", "label": "charge", "kind": "function",
         "source_file": "billing/charge.py", "source_location": "40"},
        {"id": "stripe_create", "label": "Stripe.create", "kind": "function",
         "source_file": "billing/stripe.py"},
        {"id": "retry_job", "label": "RetryJob", "kind": "class",
         "source_file": "billing/retry.py"},
        {"id": "db", "label": "Database", "kind": "module",
         "source_file": "core/db.py"},
    ],
    "links": [
        {"source": "subscribebutton", "target": "apiclient_post", "relation": "calls"},
        {"source": "apiclient_post", "target": "charge", "relation": "calls"},
        {"source": "charge", "target": "stripe_create", "relation": "calls"},
        {"source": "retry_job", "target": "charge", "relation": "calls"},
        {"source": "charge", "target": "db", "relation": "uses"},
        {"source": "retry_job", "target": "db", "relation": "uses"},
    ],
}


@pytest.fixture()
def graph():
    return q.load_graph_from_json(json.dumps(_FIXTURE))


class TestLoadGraph:
    def test_loads_directed_with_links_key(self, graph):
        assert graph.is_directed()
        assert graph.number_of_nodes() == 6
        assert graph.number_of_edges() == 6

    def test_legacy_edges_key_accepted(self):
        legacy = dict(_FIXTURE)
        legacy["edges"] = legacy.pop("links")
        G = q.load_graph_from_json(json.dumps(legacy))
        assert G.number_of_edges() == 6

    def test_bad_json_raises_value_error_never_exits(self):
        with pytest.raises(ValueError):
            q.load_graph_from_json("{nope")
        with pytest.raises(ValueError):
            q.load_graph_from_json('"a bare string"')


class TestFindNodes:
    def test_exact_label_ranks_first_with_metadata(self, graph):
        hits = q.find_nodes(graph, "charge")
        assert hits[0]["id"] == "charge"
        assert hits[0]["source_file"] == "billing/charge.py"
        assert hits[0]["degree"] == 4  # 2 in (apiclient, retry) + 2 out (stripe, db)

    def test_limit_caps_results(self, graph):
        assert len(q.find_nodes(graph, "e", limit=2)) <= 2

    def test_miss_returns_empty(self, graph):
        assert q.find_nodes(graph, "zzz_nothing") == []


class TestResolveNodeId:
    def test_exact_id_and_label_and_fuzzy(self, graph):
        assert q.resolve_node_id(graph, "charge") == "charge"
        assert q.resolve_node_id(graph, "SubscribeButton") == "subscribebutton"
        assert q.resolve_node_id(graph, "RetryJ") == "retry_job"  # top find_nodes hit

    def test_total_miss_is_none(self, graph):
        assert q.resolve_node_id(graph, "zzz_nothing") is None


class TestQueryGraphText:
    def test_returns_seeded_subgraph_text(self, graph):
        text = q.query_graph_text(graph, "how does charge work", token_budget=2000)
        assert "charge" in text.lower()
        assert "NODE" in text  # serve's subgraph rendering shape

    def test_no_match_is_a_message_not_an_error(self, graph):
        assert "No matching nodes" in q.query_graph_text(graph, "zzz qqq nothing")


class TestNeighborsText:
    def test_count_first_header_with_relation_breakdown(self, graph):
        text = q.neighbors_text(graph, "charge")
        header = text.splitlines()[0]
        assert "2 incoming / 2 outgoing" in header
        assert "calls: 3" in text  # 2 in + 1 out of relation 'calls'
        assert "uses: 1" in text

    def test_god_node_listing_is_capped_with_a_narrowing_hint(self, graph):
        text = q.neighbors_text(graph, "charge", limit=1)
        assert "+3 more" in text
        assert "relation=" in text

    def test_relation_and_direction_filters(self, graph):
        text = q.neighbors_text(graph, "charge", relation="uses", direction="out")
        assert "Database" in text
        assert "Stripe.create" not in text

    def test_unknown_node_points_at_search(self, graph):
        assert "node search" in q.neighbors_text(graph, "zzz_nothing")


class TestShortestPathText:
    def test_directed_path_with_relations(self, graph):
        text = q.shortest_path_text(graph, "SubscribeButton", "Stripe.create")
        assert "3 hops" in text
        assert "--calls-->" in text
        assert text.index("SubscribeButton") < text.index("Stripe.create")

    def test_reverse_direction_falls_back_undirected_and_says_so(self, graph):
        text = q.shortest_path_text(graph, "Stripe.create", "SubscribeButton")
        assert "ignoring edge direction" in text

    def test_disconnected_says_no_path(self):
        iso = dict(_FIXTURE, nodes=_FIXTURE["nodes"] + [{"id": "island", "label": "Island"}])
        G = q.load_graph_from_json(json.dumps(iso))
        assert "No path" in q.shortest_path_text(G, "Island", "charge")


class TestBlastRadius:
    def test_reverse_bfs_finds_transitive_callers(self, graph):
        text = q.blast_radius_text(graph, "charge", depth=2)
        # direct callers + the transitive FE chain, with via-relations.
        assert "apiClient.post" in text
        assert "RetryJob" in text
        assert "SubscribeButton" in text
        assert "[calls]" in text

    def test_downstream_nodes_are_not_in_the_impact_set(self, graph):
        text = q.blast_radius_text(graph, "charge", depth=2)
        assert "Stripe.create" not in text  # charge CALLS it; it doesn't depend on charge

    def test_ambiguous_or_missing_seed_is_a_message(self, graph):
        assert "No unique node match" in q.blast_radius_text(graph, "zzz_nothing")
