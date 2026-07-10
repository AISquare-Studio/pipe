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
        assert "cluster-only" not in commands  # AST path: update writes the report itself
        assert art.tier == "ast"
        assert art.enrichment_error is None

    def test_enriched_build_passes_explicit_backend_and_model(self, stub, checkout):
        art = _engine(stub, backend="claude", api_key="sk-test").build(checkout)
        extract = next(c for c in stub.calls() if c["argv"][0] == "extract")
        assert extract["argv"] == [
            "extract", ".", "--no-viz", "--backend", "claude", "--model", "claude-haiku-4-5",
        ]
        assert art.tier == "enriched"

    def test_explicit_model_overrides_default(self, stub, checkout):
        _engine(stub, backend="claude", api_key="sk-test", model="claude-sonnet-4-6").build(checkout)
        extract = next(c for c in stub.calls() if c["argv"][0] == "extract")
        assert extract["argv"][-2:] == ["--model", "claude-sonnet-4-6"]

    def test_keyless_backend_ollama_is_armed_without_key(self, stub, checkout):
        art = _engine(stub, backend="ollama").build(checkout)
        extract = next(c for c in stub.calls() if c["argv"][0] == "extract")
        # The Haiku default is claude-only — ollama must NOT inherit a claude
        # model id; with no explicit model the flag is omitted entirely.
        assert extract["argv"][-2:] == ["--backend", "ollama"]
        assert "--model" not in extract["argv"]
        assert art.tier == "enriched"

    def test_keyed_backend_without_key_is_config_error(self):
        with pytest.raises(ConfigValidationError):
            GraphifyEngine(backend="claude")


class TestClusterStep:
    """extract writes only graph.json on real graphifyy — the report comes from
    cluster-only. These paths were masked by the old self-writing stub."""

    def test_enriched_runs_extract_then_cluster_only(self, stub, checkout):
        art = _engine(stub, backend="claude", api_key="sk-test").build(checkout)
        commands = [c["argv"][0] for c in stub.calls() if c["argv"][0] != "--version"]
        assert commands == ["extract", "cluster-only"]
        cluster = next(c for c in stub.calls() if c["argv"][0] == "cluster-only")
        # cluster-only parses --backend only in `=` form (verified 0.8.36).
        assert "--backend=claude" in cluster["argv"]
        assert cluster["env"].get("ANTHROPIC_API_KEY") == "sk-test"  # labeling needs the key
        assert art.tier == "enriched"
        assert art.report_md.startswith("# Graph report")

    def test_cluster_failure_degrades_to_ast(self, stub, checkout):
        stub.touch("FAIL_CLUSTER")
        art = _engine(stub, backend="claude", api_key="sk-test").build(checkout)
        commands = [c["argv"][0] for c in stub.calls() if c["argv"][0] != "--version"]
        assert commands == ["extract", "cluster-only", "update"]
        assert art.tier == "ast"
        assert "cluster boom" in (art.enrichment_error or "")
        assert art.report_md.startswith("# Graph report")  # salvaged by update

    def test_cluster_succeeds_but_no_report_degrades_to_ast(self, stub, checkout):
        # Belt-and-braces against engine drift: every command exits 0 yet no
        # usable report exists — the build must salvage AST, never raise after
        # having paid the LLM pass.
        stub.touch("CLUSTER_NO_REPORT")
        art = _engine(stub, backend="claude", api_key="sk-test").build(checkout)
        commands = [c["argv"][0] for c in stub.calls() if c["argv"][0] != "--version"]
        assert commands == ["extract", "cluster-only", "update"]
        assert art.tier == "ast"
        assert "no usable GRAPH_REPORT.md" in (art.enrichment_error or "")


class TestTelemetry:
    def test_enriched_build_parses_stdout_spend(self, stub, checkout):
        art = _engine(stub, backend="claude", api_key="sk-test").build(checkout)
        assert art.llm_tokens_in == 1234
        assert art.llm_tokens_out == 567
        assert art.llm_cost_estimate == pytest.approx(0.0123)

    def test_ast_build_records_zero_spend(self, stub, checkout):
        art = _engine(stub).build(checkout)
        assert (art.llm_tokens_in, art.llm_tokens_out, art.llm_cost_estimate) == (0, 0, 0.0)

    def test_analysis_json_is_the_fallback_source(self, stub, tmp_path):
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        (out_dir / ".graphify_analysis.json").write_text(
            '{"tokens": {"input": 9000, "output": 400}}'
        )
        engine = _engine(stub)
        assert engine._parse_llm_telemetry("no spend line here", str(out_dir)) == (9000, 400, 0.0)

    def test_degraded_build_still_records_sunk_spend(self, stub, checkout):
        # extract paid the LLM, cluster-only died — the spend must be visible.
        stub.touch("FAIL_CLUSTER")
        art = _engine(stub, backend="claude", api_key="sk-test").build(checkout)
        assert art.tier == "ast"
        assert art.llm_tokens_in == 1234


class TestDocVolumeCap:
    def test_doc_heavy_checkout_degrades_without_spawning_extract(self, stub, checkout):
        with open(f"{checkout}/HUGE.md", "w") as fh:
            fh.write("x" * 4096)
        art = _engine(
            stub, backend="claude", api_key="sk-test", doc_volume_cap_bytes=1024
        ).build(checkout)
        commands = [c["argv"][0] for c in stub.calls() if c["argv"][0] != "--version"]
        assert "extract" not in commands  # never burned the LLM pass
        assert art.tier == "ast"
        assert "doc volume" in (art.enrichment_error or "")

    def test_cap_zero_disables_the_preflight(self, stub, checkout):
        with open(f"{checkout}/HUGE.md", "w") as fh:
            fh.write("x" * 4096)
        art = _engine(
            stub, backend="claude", api_key="sk-test", doc_volume_cap_bytes=0
        ).build(checkout)
        assert art.tier == "enriched"

    def test_code_only_checkout_never_trips_the_cap(self, stub, checkout):
        # checkout fixture holds only app.py — code is AST-only, zero doc bytes.
        art = _engine(
            stub, backend="claude", api_key="sk-test", doc_volume_cap_bytes=1
        ).build(checkout)
        assert art.tier == "enriched"


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


class TestIncrementalState:
    """V2 Phase 3: capture-after-build + restore-before-build round trip."""

    def test_build_captures_state_tar_with_graph_json(self, stub, checkout):
        import io
        import tarfile

        art = _engine(stub).build(checkout)
        assert art.state_tar  # update wrote graph.json + report → state exists
        with tarfile.open(fileobj=io.BytesIO(art.state_tar), mode="r:gz") as tar:
            names = tar.getnames()
        assert "graphify-out/graph.json" in names

    def test_prior_state_is_restored_before_the_run(self, stub, checkout, tmp_path):
        # Build once to capture state, then feed it into a FRESH checkout and
        # verify the restored files exist there after the second build.
        art = _engine(stub).build(checkout)
        fresh = tmp_path / "fresh"
        fresh.mkdir()
        (fresh / "app.py").write_text("def main():\n    return 1\n")
        _engine(stub).build(str(fresh), prior_state=art.state_tar)
        assert (fresh / "graphify-out" / "graph.json").exists()

    def test_corrupt_prior_state_degrades_to_full_build(self, stub, checkout):
        art = _engine(stub).build(checkout, prior_state=b"definitely-not-a-tar")
        assert art.report_md.startswith("# Graph report")  # build still succeeded

    def test_hostile_member_paths_never_escape_the_checkout(self, stub, checkout, tmp_path):
        import io
        import tarfile

        evil = io.BytesIO()
        with tarfile.open(fileobj=evil, mode="w:gz") as tar:
            payload = io.BytesIO(b"pwn")
            info = tarfile.TarInfo(name="../escape.txt")
            info.size = 3
            tar.addfile(info, payload)
        art = _engine(stub).build(checkout, prior_state=evil.getvalue())
        assert art.report_md  # build fine
        assert not (tmp_path / "escape.txt").exists()  # nothing escaped
