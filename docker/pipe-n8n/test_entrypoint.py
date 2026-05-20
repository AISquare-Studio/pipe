"""Tests for the Docker entrypoint's env-var -> config translation.

Run from the repo root:
    .venv/bin/python -m pytest docker/pipe-n8n/test_entrypoint.py -v
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "pipe_n8n_entrypoint",
    Path(__file__).parent / "entrypoint.py",
)
entrypoint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(entrypoint)


@pytest.fixture
def clean_env(monkeypatch):
    for var in (
        "N8N_URL",
        "N8N_API_KEY",
        "N8N_POLL_INTERVAL",
        "N8N_WORKFLOW_FILTER",
        "N8N_CURSOR_PATH",
        "AISQUARE_GATEWAY_URL",
        "AISQUARE_API_KEY",
        "AISQUARE_INGEST_PATH",
        "AISQUARE_TIMEOUT_SECONDS",
        "AISQUARE_MAX_RETRIES",
    ):
        monkeypatch.delenv(var, raising=False)


class TestBuildConfig:
    def test_minimal_required_env(self, clean_env, monkeypatch):
        monkeypatch.setenv("N8N_URL", "http://n8n:5678")
        monkeypatch.setenv("N8N_API_KEY", "n-key")
        monkeypatch.setenv("AISQUARE_GATEWAY_URL", "https://gw")
        monkeypatch.setenv("AISQUARE_API_KEY", "g-key")
        cfg = entrypoint._build_config()
        assert cfg == {
            "n8n": {"n8n_url": "http://n8n:5678", "api_key": "n-key"},
            "aisquare-gateway": {"gateway_url": "https://gw", "api_key": "g-key"},
        }

    def test_optional_overrides_propagate(self, clean_env, monkeypatch):
        monkeypatch.setenv("N8N_URL", "http://n8n")
        monkeypatch.setenv("N8N_API_KEY", "nk")
        monkeypatch.setenv("AISQUARE_GATEWAY_URL", "http://gw")
        monkeypatch.setenv("AISQUARE_API_KEY", "gk")
        monkeypatch.setenv("N8N_POLL_INTERVAL", "30")
        monkeypatch.setenv("N8N_WORKFLOW_FILTER", "wf-1, wf-2 ,wf-3")
        monkeypatch.setenv("N8N_CURSOR_PATH", "/data/cursor.json")
        monkeypatch.setenv("AISQUARE_INGEST_PATH", "/v2/ingest")
        monkeypatch.setenv("AISQUARE_TIMEOUT_SECONDS", "20")
        monkeypatch.setenv("AISQUARE_MAX_RETRIES", "5")
        cfg = entrypoint._build_config()
        assert cfg["n8n"]["poll_interval_seconds"] == 30
        assert cfg["n8n"]["workflow_id_filter"] == ["wf-1", "wf-2", "wf-3"]
        assert cfg["n8n"]["cursor_path"] == "/data/cursor.json"
        assert cfg["aisquare-gateway"]["ingest_path"] == "/v2/ingest"
        assert cfg["aisquare-gateway"]["timeout_seconds"] == 20
        assert cfg["aisquare-gateway"]["max_retries"] == 5


class TestMissingVars:
    def test_missing_required_fails_with_clear_message(self, clean_env, capsys):
        with pytest.raises(SystemExit) as excinfo:
            entrypoint._build_config()
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "N8N_URL" in err
        assert "N8N_API_KEY" in err
        assert "AISQUARE_GATEWAY_URL" in err
        assert "AISQUARE_API_KEY" in err

    def test_missing_one_required(self, clean_env, monkeypatch, capsys):
        monkeypatch.setenv("N8N_URL", "x")
        monkeypatch.setenv("AISQUARE_GATEWAY_URL", "y")
        monkeypatch.setenv("AISQUARE_API_KEY", "z")
        with pytest.raises(SystemExit):
            entrypoint._build_config()
        err = capsys.readouterr().err
        assert "N8N_API_KEY" in err

    def test_invalid_integer_fails(self, clean_env, monkeypatch, capsys):
        monkeypatch.setenv("N8N_URL", "x")
        monkeypatch.setenv("N8N_API_KEY", "y")
        monkeypatch.setenv("AISQUARE_GATEWAY_URL", "z")
        monkeypatch.setenv("AISQUARE_API_KEY", "w")
        monkeypatch.setenv("N8N_POLL_INTERVAL", "not-a-number")
        with pytest.raises(SystemExit):
            entrypoint._build_config()
        err = capsys.readouterr().err
        assert "N8N_POLL_INTERVAL" in err
