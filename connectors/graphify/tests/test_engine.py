"""GraphifyEngine — tier selection, env scrubbing, fallback, sanity gates.

Drives the real subprocess path against the recording fake `graphify` stub
(conftest) — no real graphify, no network (preflight disabled or mocked).
"""

from __future__ import annotations

from unittest import mock

import pytest

from aisquare.pipe.errors import ConfigValidationError, PipelineError
from aisquare_pipe_graphify import engine as engine_mod
from aisquare_pipe_graphify.engine import GraphifyEngine, parse_graph_stats


def _engine(stub, **kwargs):
    kwargs.setdefault("graphify_bin", str(stub.bin))
    kwargs.setdefault("preflight", False)
    return GraphifyEngine(**kwargs)


class TestTierSelection:
    def test_keyless_build_uses_update_never_extract(self, stub, checkout, scrub_canary):
        art = _engine(stub).build(checkout)
        commands = [c["argv"][0] for c in stub.calls() if c["argv"] and c["argv"][0] != "--version"]
        assert "update" in commands
        assert "extract" not in commands  # C1: a backend-less extract must never be spawned
        assert art.tier == "ast"
        assert art.enrichment_error is None

    def test_enriched_build_passes_explicit_backend(self, stub, checkout):
        art = _engine(stub, backend="claude", api_key="sk-test").build(checkout)
        extract = next(c for c in stub.calls() if c["argv"][0] == "extract")
        assert extract["argv"] == ["extract", ".", "--no-viz", "--backend", "claude"]
        assert art.tier == "enriched"

    def test_keyless_backend_ollama_is_armed_without_key(self, stub, checkout):
        art = _engine(stub, backend="ollama").build(checkout)
        extract = next(c for c in stub.calls() if c["argv"][0] == "extract")
        assert extract["argv"][-2:] == ["--backend", "ollama"]
        assert art.tier == "enriched"

    def test_keyed_backend_without_key_is_config_error(self):
        with pytest.raises(ConfigValidationError):
            GraphifyEngine(backend="claude")


class TestEnvScrub:
    def test_extract_env_has_only_the_intended_key(self, stub, checkout, scrub_canary):
        _engine(stub, backend="claude", api_key="sk-real").build(checkout)
        extract = next(c for c in stub.calls() if c["argv"][0] == "extract")
        assert extract["env"].get("ANTHROPIC_API_KEY") == "sk-real"  # ours, not ambient
        assert "GOOGLE_API_KEY" not in extract["env"]
        assert "AWS_REGION" not in extract["env"]

    def test_ast_tier_env_carries_no_key_at_all(self, stub, checkout, scrub_canary):
        _engine(stub).build(checkout)
        update = next(c for c in stub.calls() if c["argv"][0] == "update")
        assert "ANTHROPIC_API_KEY" not in update["env"]
        assert "GOOGLE_API_KEY" not in update["env"]


class TestFallback:
    def test_enriched_failure_falls_back_to_ast_with_error_recorded(self, stub, checkout):
        stub.touch("FAIL_EXTRACT")
        art = _engine(stub, backend="claude", api_key="sk-test").build(checkout)
        commands = [c["argv"][0] for c in stub.calls() if c["argv"][0] != "--version"]
        assert commands == ["extract", "update"]
        assert art.tier == "ast"
        assert "credit balance" in (art.enrichment_error or "")

    def test_fallback_disabled_raises_with_stderr_tail(self, stub, checkout):
        stub.touch("FAIL_EXTRACT")
        with pytest.raises(PipelineError) as exc:
            _engine(
                stub, backend="claude", api_key="sk-test", fallback_to_ast=False
            ).build(checkout)
        assert "credit balance" in str(exc.value)

    def test_failed_preflight_skips_extract_and_falls_back(self, stub, checkout):
        resp = mock.Mock(status_code=401, text="unauthorized")
        with mock.patch.object(engine_mod.requests, "post", return_value=resp):
            art = GraphifyEngine(
                backend="claude", api_key="sk-dead", graphify_bin=str(stub.bin), preflight=True
            ).build(checkout)
        commands = [c["argv"][0] for c in stub.calls() if c["argv"][0] != "--version"]
        assert "extract" not in commands  # never burned the 25-min budget on a dead key
        assert art.tier == "ast"
        assert "preflight" in (art.enrichment_error or "")

    def test_preflight_network_flakiness_does_not_block(self, stub, checkout):
        with mock.patch.object(engine_mod.requests, "post", side_effect=OSError("dns down")):
            art = GraphifyEngine(
                backend="claude", api_key="sk-test", graphify_bin=str(stub.bin), preflight=True
            ).build(checkout)
        assert art.tier == "enriched"  # advisory probe — proceed on probe failure


class TestSanityGates:
    def test_missing_report_raises(self, stub, checkout):
        stub.touch("NO_ARTIFACTS")
        with pytest.raises(PipelineError) as exc:
            _engine(stub).build(checkout)
        assert "no usable GRAPH_REPORT.md" in str(exc.value)

    def test_unparseable_graph_json_raises(self, stub, checkout):
        stub.touch("BAD_JSON")
        with pytest.raises(PipelineError) as exc:
            _engine(stub).build(checkout)
        assert "unparseable" in str(exc.value)

    def test_missing_binary_is_instructive(self, checkout):
        with pytest.raises(PipelineError) as exc:
            GraphifyEngine(graphify_bin="/nope/graphify", preflight=False).build(checkout)
        assert "not found" in str(exc.value)


class TestArtifacts:
    def test_stats_tokens_and_version(self, stub, checkout):
        art = _engine(stub).build(checkout)
        assert (art.nodes, art.edges, art.communities) == (12, 34, 3)
        assert art.token_count > 0
        assert "0.8.36-fake" in art.graphify_version
        assert art.report_md.startswith("# Graph report")
        assert '"nodes"' in art.graph_json


class TestParseGraphStats:
    def test_falls_back_to_node_link_counts(self):
        nodes, edges, communities = parse_graph_stats(
            "no numbers here", '{"nodes": [1, 2], "links": [1]}'
        )
        assert (nodes, edges, communities) == (2, 1, 0)

    def test_bad_json_is_swallowed(self):
        assert parse_graph_stats("", "{nope") == (0, 0, 0)
