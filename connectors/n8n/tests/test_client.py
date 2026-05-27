"""Tests for the N8nClient HTTP wrapper."""

from __future__ import annotations

import pytest

from aisquare.pipe.errors import ConfigValidationError, PipelineError

from aisquare_pipe_n8n.client import N8nClient, load_cursor, save_cursor

from tests.conftest import API_KEY
from tests.helpers import make_execution


class TestClientInit:
    def test_init_requires_url(self):
        with pytest.raises(ConfigValidationError, match="n8n_url"):
            N8nClient({"api_key": "x"})

    def test_init_requires_api_key(self):
        with pytest.raises(ConfigValidationError, match="api_key"):
            N8nClient({"n8n_url": "http://x"})

    def test_init_strips_trailing_slash(self):
        c = N8nClient({"n8n_url": "http://x/", "api_key": "k"})
        assert c._base_url == "http://x"


class TestClientListExecutions:
    def test_returns_only_new_executions(self, n8n_config):
        server = n8n_config["_server"]
        server.executions = [
            make_execution(execution_id=1),
            make_execution(execution_id=2),
            make_execution(execution_id=3),
        ]
        client = N8nClient(n8n_config)
        result = client.list_executions(last_id=1)
        ids = [int(e["id"]) for e in result]
        assert ids == [2, 3]

    def test_returns_all_when_no_cursor(self, n8n_config):
        server = n8n_config["_server"]
        server.executions = [make_execution(execution_id=i) for i in (1, 2, 3)]
        result = N8nClient(n8n_config).list_executions(last_id=0)
        assert [int(e["id"]) for e in result] == [1, 2, 3]

    def test_workflow_filter(self, n8n_config):
        server = n8n_config["_server"]
        server.executions = [
            make_execution(execution_id=1, workflow_id="wf-1"),
            make_execution(execution_id=2, workflow_id="wf-2"),
            make_execution(execution_id=3, workflow_id="wf-1"),
        ]
        result = N8nClient(n8n_config).list_executions(
            last_id=0, workflow_ids=["wf-1"]
        )
        assert {int(e["id"]) for e in result} == {1, 3}

    def test_sends_api_key_header(self, n8n_config):
        server = n8n_config["_server"]
        server.executions = []
        N8nClient(n8n_config).list_executions(last_id=0)
        path, _, headers = server.requests[-1]
        assert path == "/api/v1/executions"
        assert headers["X-N8N-API-KEY"] == API_KEY

    def test_unauthorized_raises(self, n8n_config):
        n8n_config["api_key"] = "wrong"
        with pytest.raises(PipelineError, match="HTTP 401"):
            N8nClient(n8n_config).list_executions(last_id=0)

    def test_network_failure_raises(self, tmp_path):
        client = N8nClient(
            {"n8n_url": "http://127.0.0.1:1", "api_key": "k", "request_timeout_seconds": 1}
        )
        with pytest.raises(PipelineError, match="failed"):
            client.list_executions(last_id=0)


class TestClientValidate:
    def test_valid(self, n8n_config):
        assert N8nClient(n8n_config).validate() is True

    def test_invalid_credentials_raise(self, n8n_config):
        n8n_config["api_key"] = "nope"
        with pytest.raises(PipelineError):
            N8nClient(n8n_config).validate()


class TestCursorPersistence:
    def test_load_missing_file_returns_zero(self, tmp_path):
        assert load_cursor(str(tmp_path / "no.json")) == 0

    def test_round_trip(self, tmp_path):
        path = str(tmp_path / "cursor.json")
        save_cursor(path, 42)
        assert load_cursor(path) == 42

    def test_load_corrupt_file_returns_zero(self, tmp_path):
        path = tmp_path / "cursor.json"
        path.write_text("not json")
        assert load_cursor(str(path)) == 0

    def test_save_creates_parent_dirs(self, tmp_path):
        path = str(tmp_path / "deep" / "nested" / "cursor.json")
        save_cursor(path, 7)
        assert load_cursor(path) == 7
