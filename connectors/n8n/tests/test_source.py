"""Tests for N8nSource — the polling generator.

Each yielded envelope carries one TraceBatch (``{trace_id, spans: [...]}``)
for one logical emission (stub / progress / final) of an execution.
"""

from __future__ import annotations

import json

import pytest

from aisquare.pipe.core.envelope import PullParams

from aisquare_pipe_n8n.connector import (
    IDEMPOTENCY_PREFIX_FINAL,
    IDEMPOTENCY_PREFIX_PROGRESS,
    IDEMPOTENCY_PREFIX_STUB,
    TRACE_CONTENT_TYPE,
    N8nSource,
)

from tests.helpers import make_execution, make_workflow_def


def _pull_once(source: N8nSource, config: dict, **extra) -> list:
    params = PullParams(params={"max_polls": 1, "sleep": lambda _: None, **extra})
    return list(source.pull(config, params))


def _spans(envelope) -> list:
    return envelope.data["spans"]


def _attrs_for(envelope, predicate) -> dict:
    for span in _spans(envelope):
        if predicate(span):
            return span.get("attributes") or {}
    raise AssertionError("no span matched predicate")


class TestN8nSourceFinishedExecutions:
    """Final emission for a finished execution."""

    def test_emits_one_envelope_per_finished_execution(self, n8n_config):
        n8n_config["_server"].executions = [
            make_execution(execution_id=1),
            make_execution(execution_id=2),
        ]
        envelopes = _pull_once(N8nSource(), n8n_config)
        assert len(envelopes) == 2

    def test_envelope_carries_trace_batch_shape(self, n8n_config):
        n8n_config["_server"].executions = [make_execution(execution_id=1)]
        (env,) = _pull_once(N8nSource(), n8n_config)
        assert env.content_type == TRACE_CONTENT_TYPE
        assert isinstance(env.data, dict)
        assert "trace_id" in env.data
        assert "spans" in env.data
        assert isinstance(env.data["spans"], list)
        assert len(env.data["spans"]) >= 3  # start + at least one node_step + complete

    def test_span_structure_start_nodes_complete(self, n8n_config):
        n8n_config["_server"].executions = [make_execution(execution_id=1)]
        (env,) = _pull_once(N8nSource(), n8n_config)

        spans = _spans(env)
        events = [(s.get("attributes") or {}).get("n8n.event") for s in spans]
        assert events[0] == "workflow_start"
        assert events[-1] == "workflow_complete"
        assert any(e == "node_step" for e in events[1:-1])

    def test_root_span_has_agent_metadata(self, n8n_config):
        n8n_config["_server"].executions = [
            make_execution(execution_id=5, workflow_name="The Flow")
        ]
        (env,) = _pull_once(N8nSource(), n8n_config)

        attrs = _attrs_for(env, lambda s: s["attributes"]["n8n.event"] == "workflow_start")
        assert attrs["agent.name"] == "The Flow"
        assert attrs["agent.metadata.source"] == "n8n"
        assert attrs["openinference.span.kind"] == "AGENT"
        assert attrs["agent.metadata.run_kind"] == "n8n_execution"
        assert attrs["agent.metadata.session.id"] == "n8n-workflow-wf-1"
        assert attrs["agent.metadata.run.title"] == "The Flow · execution #5"

    def test_llm_node_carries_openinference_attrs(self, n8n_config):
        n8n_config["_server"].executions = [make_execution(execution_id=1)]
        (env,) = _pull_once(N8nSource(), n8n_config)

        # The seed has an "AI Agent" node which the classifier flags as LLM.
        attrs = _attrs_for(
            env,
            lambda s: (s.get("attributes") or {}).get("n8n.node_name") == "AI Agent",
        )
        assert attrs["openinference.span.kind"] == "LLM"
        assert "llm.system" in attrs

    def test_timestamps_are_nanos(self, n8n_config):
        n8n_config["_server"].executions = [make_execution(execution_id=1)]
        (env,) = _pull_once(N8nSource(), n8n_config)

        for span in _spans(env):
            # Spans must have ns timestamps for the gateway's structural worker;
            # in particular start_time is mandatory on every emitted span.
            assert isinstance(span["start_time"], int), span

    def test_metadata_carries_idempotency_key_final(self, n8n_config):
        n8n_config["_server"].executions = [make_execution(execution_id=5)]
        (env,) = _pull_once(N8nSource(), n8n_config)

        assert env.metadata["n8n_event"] == "final"
        assert env.metadata["idempotency_key"].startswith(IDEMPOTENCY_PREFIX_FINAL + ":")
        assert env.metadata["n8n_execution_id"] == "5"


class TestN8nSourceRunningExecutions:
    """Stub + progress emissions for in-progress executions."""

    def test_stub_envelope_emitted_for_running_execution(self, n8n_config):
        server = n8n_config["_server"]
        server.workflow_defs["wf-1"] = make_workflow_def(node_names=["A", "B"])
        server.executions = [
            make_execution(
                execution_id=1,
                finished=False,
                stopped_at=None,
                nodes={},  # no runData yet — pure stub
            )
        ]
        envelopes = _pull_once(N8nSource(), n8n_config)

        stubs = [e for e in envelopes if e.metadata["n8n_event"] == "stub"]
        assert len(stubs) == 1
        stub = stubs[0]
        assert stub.metadata["idempotency_key"].startswith(
            IDEMPOTENCY_PREFIX_STUB + ":"
        )
        # One root span + one pending span per node in the workflow def
        events = [
            (s.get("attributes") or {}).get("n8n.event")
            for s in _spans(stub)
        ]
        assert events.count("workflow_start") == 1
        assert events.count("node_step") == 2

    def test_progress_envelope_when_partial_run_data_present(self, n8n_config):
        server = n8n_config["_server"]
        server.workflow_defs["wf-1"] = make_workflow_def(node_names=["Start", "AI Agent"])
        # In-progress run with partial runData — Start completed, AI Agent still running.
        server.executions = [
            make_execution(
                execution_id=1,
                finished=False,
                stopped_at=None,
                nodes={
                    "Start": [
                        {
                            "startTime": 1704067200000,
                            "executionTime": 5,
                            "data": {"main": [[{"json": {"ok": True}}]]},
                            "source": [],
                        }
                    ],
                },
            )
        ]
        envelopes = _pull_once(N8nSource(), n8n_config)

        events_seen = {e.metadata["n8n_event"] for e in envelopes}
        assert "stub" in events_seen
        assert "progress" in events_seen

        progress = next(e for e in envelopes if e.metadata["n8n_event"] == "progress")
        assert progress.metadata["idempotency_key"].startswith(
            IDEMPOTENCY_PREFIX_PROGRESS + ":"
        )
        # progress idempotency key incorporates a fingerprint so steady-state polls dedupe.
        assert progress.metadata["idempotency_key"].count(":") >= 3

    def test_unfinished_run_does_not_advance_cursor(self, n8n_config, tmp_path):
        """Critical: cursor must only move past finished executions, otherwise
        the eventual finished-state emission would be skipped."""
        server = n8n_config["_server"]
        server.executions = [
            make_execution(execution_id=10, finished=False, stopped_at=None),
        ]
        _pull_once(N8nSource(), n8n_config)
        # Either no cursor file written (no finished executions) or its value is 0.
        from pathlib import Path
        p = Path(n8n_config["cursor_path"])
        if p.exists():
            assert json.loads(p.read_text())["last_execution_id"] == 0

    def test_include_running_false_skips_in_progress(self, n8n_config):
        server = n8n_config["_server"]
        server.executions = [
            make_execution(execution_id=1, finished=False, stopped_at=None),
        ]
        n8n_config["include_running"] = False
        envelopes = _pull_once(N8nSource(), n8n_config)
        assert envelopes == []


class TestWorkflowFilter:
    def test_workflow_filter_applied_to_finished_phase(self, n8n_config):
        server = n8n_config["_server"]
        server.executions = [
            make_execution(execution_id=1, workflow_id="wf-A"),
            make_execution(execution_id=2, workflow_id="wf-B"),
        ]
        n8n_config["workflow_id_filter"] = ["wf-A"]
        envelopes = _pull_once(N8nSource(), n8n_config)
        wf_ids = {e.metadata["n8n_workflow_id"] for e in envelopes}
        assert wf_ids == {"wf-A"}


class TestCursorDurability:
    def test_cursor_advances_past_finished(self, n8n_config):
        server = n8n_config["_server"]
        server.executions = [make_execution(execution_id=i) for i in (1, 2, 3)]
        _pull_once(N8nSource(), n8n_config)

        with open(n8n_config["cursor_path"]) as f:
            assert json.load(f)["last_execution_id"] == 3

    def test_restart_does_not_re_emit_finished(self, n8n_config):
        server = n8n_config["_server"]
        server.executions = [make_execution(execution_id=i) for i in (1, 2)]

        first = _pull_once(N8nSource(), n8n_config)
        assert first, "first pull should emit envelopes"

        # Same executions still present; a second pull must see nothing new.
        second = _pull_once(N8nSource(), n8n_config)
        assert second == []

        server.executions.append(make_execution(execution_id=3))
        third = _pull_once(N8nSource(), n8n_config)
        assert third, "new execution should be emitted"
        assert all(e.metadata["n8n_execution_id"] == "3" for e in third)

    def test_no_executions_writes_no_cursor(self, n8n_config):
        n8n_config["_server"].executions = []
        _pull_once(N8nSource(), n8n_config)
        from pathlib import Path
        assert not Path(n8n_config["cursor_path"]).exists()


class TestPollLoop:
    def test_respects_max_polls(self, n8n_config):
        server = n8n_config["_server"]
        sleeps: list[float] = []

        def fake_sleep(s: float) -> None:
            sleeps.append(s)
            server.executions.append(
                make_execution(execution_id=len(server.executions) + 1)
            )

        server.executions = [make_execution(execution_id=1)]
        params = PullParams(params={"max_polls": 3, "sleep": fake_sleep})
        envelopes = list(N8nSource().pull(n8n_config, params))

        # Three polls = two sleeps in between.
        assert len(sleeps) == 2
        exec_ids = {e.metadata["n8n_execution_id"] for e in envelopes}
        assert exec_ids == {"1", "2", "3"}


class TestValidateConfig:
    def test_missing_config(self):
        assert N8nSource().validate_config({}) is False

    def test_valid(self, n8n_config):
        assert N8nSource().validate_config(n8n_config) is True

    def test_bad_credentials(self, n8n_config):
        n8n_config["api_key"] = "wrong"
        assert N8nSource().validate_config(n8n_config) is False
