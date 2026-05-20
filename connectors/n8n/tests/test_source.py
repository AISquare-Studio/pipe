"""Tests for N8nSource — the polling generator."""

from __future__ import annotations

import json

import pytest

from aisquare.pipe.core.envelope import PullParams

from aisquare_pipe_n8n.source import (
    EVENT_NODE_STEP,
    EVENT_WORKFLOW_COMPLETE,
    EVENT_WORKFLOW_START,
    TRACE_CONTENT_TYPE,
    N8nSource,
)

from tests.helpers import make_execution


def _pull_once(source: N8nSource, config: dict, **extra) -> list:
    params = PullParams(params={"max_polls": 1, "sleep": lambda _: None, **extra})
    return list(source.pull(config, params))


class TestN8nSourcePull:
    def test_emits_start_step_complete_in_order(self, n8n_config):
        n8n_config["_server"].executions = [make_execution(execution_id=1)]
        envelopes = _pull_once(N8nSource(), n8n_config)

        events = [e.data["event"] for e in envelopes]
        # start, then one or more node_steps, then complete
        assert events[0] == EVENT_WORKFLOW_START
        assert events[-1] == EVENT_WORKFLOW_COMPLETE
        assert all(ev == EVENT_NODE_STEP for ev in events[1:-1])
        assert events.count(EVENT_NODE_STEP) >= 1

    def test_all_envelopes_share_trace_content_type(self, n8n_config):
        n8n_config["_server"].executions = [make_execution(execution_id=1)]
        envelopes = _pull_once(N8nSource(), n8n_config)
        assert {e.content_type for e in envelopes} == {TRACE_CONTENT_TYPE}

    def test_metadata_carries_execution_workflow_ids(self, n8n_config):
        n8n_config["_server"].executions = [
            make_execution(execution_id=5, workflow_id="wf-X", workflow_name="The Flow")
        ]
        envelopes = _pull_once(N8nSource(), n8n_config)
        for env in envelopes:
            assert env.metadata["n8n_execution_id"] == "5"
            assert env.metadata["n8n_workflow_id"] == "wf-X"
            assert env.metadata["n8n_workflow_name"] == "The Flow"
            assert "n8n_event" in env.metadata

    def test_node_step_surfaces_langchain_details(self, n8n_config):
        n8n_config["_server"].executions = [make_execution(execution_id=1)]
        envelopes = _pull_once(N8nSource(), n8n_config)
        ai_steps = [
            e for e in envelopes
            if e.data["event"] == EVENT_NODE_STEP and e.data.get("ai")
        ]
        assert ai_steps, "expected at least one node_step with AI details"
        ai = ai_steps[0].data["ai"]
        assert "n8n.nodes.langchain.agent" in ai

    def test_unfinished_run_omits_workflow_complete(self, n8n_config):
        n8n_config["_server"].executions = [
            make_execution(execution_id=1, finished=False, stopped_at=None)
        ]
        envelopes = _pull_once(N8nSource(), n8n_config)
        events = [e.data["event"] for e in envelopes]
        assert EVENT_WORKFLOW_START in events
        assert EVENT_WORKFLOW_COMPLETE not in events

    def test_no_executions_yields_nothing(self, n8n_config):
        n8n_config["_server"].executions = []
        envelopes = _pull_once(N8nSource(), n8n_config)
        assert envelopes == []

    def test_workflow_filter_passed_to_api(self, n8n_config):
        server = n8n_config["_server"]
        server.executions = [
            make_execution(execution_id=1, workflow_id="wf-A"),
            make_execution(execution_id=2, workflow_id="wf-B"),
        ]
        n8n_config["workflow_id_filter"] = ["wf-A"]
        envelopes = _pull_once(N8nSource(), n8n_config)
        # Every envelope should belong to wf-A
        wf_ids = {e.metadata["n8n_workflow_id"] for e in envelopes}
        assert wf_ids == {"wf-A"}


class TestCursorDurability:
    def test_cursor_advances(self, n8n_config):
        server = n8n_config["_server"]
        server.executions = [make_execution(execution_id=i) for i in (1, 2, 3)]
        _pull_once(N8nSource(), n8n_config)

        with open(n8n_config["cursor_path"]) as f:
            assert json.load(f)["last_execution_id"] == 3

    def test_restart_does_not_re_emit(self, n8n_config):
        server = n8n_config["_server"]
        server.executions = [make_execution(execution_id=i) for i in (1, 2)]

        first = _pull_once(N8nSource(), n8n_config)
        assert first, "first pull should emit envelopes"

        # Same executions still present; a second pull must see nothing new.
        second = _pull_once(N8nSource(), n8n_config)
        assert second == []

        # A new execution arrives — only that one is emitted.
        server.executions.append(make_execution(execution_id=3))
        third = _pull_once(N8nSource(), n8n_config)
        assert third, "new execution should be emitted"
        assert all(e.metadata["n8n_execution_id"] == "3" for e in third)

    def test_cursor_not_written_when_no_new_executions(self, n8n_config, tmp_path):
        n8n_config["_server"].executions = []
        _pull_once(N8nSource(), n8n_config)
        # No envelopes, no cursor file
        from pathlib import Path
        assert not Path(n8n_config["cursor_path"]).exists()


class TestPollLoop:
    def test_respects_max_polls(self, n8n_config):
        server = n8n_config["_server"]
        sleeps: list[float] = []

        def fake_sleep(s: float) -> None:
            sleeps.append(s)
            # Add a new execution between polls to confirm subsequent polls work.
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
