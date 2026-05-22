"""Tests for the GatewayClient — retry policy and headers."""

from __future__ import annotations

import pytest

from aisquare.pipe.errors import ConfigValidationError

from aisquare_pipe_gateway.client import (
    HEADER_API_KEY,
    HEADER_CONTENT_TYPE,
    HEADER_IDEMPOTENCY_KEY,
    HEADER_SOURCE_ID,
    GatewayClient,
)

from tests.conftest import API_KEY


class TestClientInit:
    def test_requires_url(self):
        with pytest.raises(ConfigValidationError, match="gateway_url"):
            GatewayClient({"api_key": "x"})

    def test_requires_api_key(self):
        with pytest.raises(ConfigValidationError, match="api_key"):
            GatewayClient({"gateway_url": "http://x"})


class TestIngestHappy:
    def test_2xx_returns_response(self, gateway_config):
        server = gateway_config["_server"]
        server.enqueue(200, {"trace_id": "abc-123"})
        client = GatewayClient(gateway_config)
        r = client.ingest({"event": "ping"}, source_id="n8n", content_type="x/y")
        assert r.status_code == 200
        assert r.trace_id == "abc-123"
        assert r.attempts == 1

    def test_sends_required_headers(self, gateway_config):
        server = gateway_config["_server"]
        server.enqueue(200, {"trace_id": "id"})
        client = GatewayClient(gateway_config)
        client.ingest({"e": 1}, source_id="n8n", content_type="text/plain")
        headers = server.received[0]["headers"]
        assert headers[HEADER_API_KEY] == API_KEY
        assert headers[HEADER_SOURCE_ID] == "n8n"
        assert headers[HEADER_CONTENT_TYPE] == "text/plain"

    def test_sends_body_as_json(self, gateway_config):
        server = gateway_config["_server"]
        server.enqueue(200, {"trace_id": "id"})
        client = GatewayClient(gateway_config)
        client.ingest({"event": "abc"}, source_id="s", content_type="t/t")
        assert server.received[0]["body"] == {"event": "abc"}

    def test_uses_custom_ingest_path(self, gateway_config):
        gateway_config["ingest_path"] = "/v2/ingest"
        server = gateway_config["_server"]
        server.enqueue(200, {"trace_id": "x"})
        GatewayClient(gateway_config).ingest(
            {"e": 1}, source_id="s", content_type="t/t"
        )
        assert server.received[0]["path"] == "/v2/ingest"

    def test_idempotency_header_set_when_provided(self, gateway_config):
        server = gateway_config["_server"]
        server.enqueue(200, {"trace_id": "x"})
        GatewayClient(gateway_config).ingest(
            {"e": 1},
            source_id="s",
            content_type="t/t",
            idempotency_key="n8n:final:abc",
        )
        headers = server.received[0]["headers"]
        assert headers[HEADER_IDEMPOTENCY_KEY] == "n8n:final:abc"

    def test_idempotency_header_omitted_when_absent(self, gateway_config):
        server = gateway_config["_server"]
        server.enqueue(200, {"trace_id": "x"})
        GatewayClient(gateway_config).ingest(
            {"e": 1}, source_id="s", content_type="t/t"
        )
        headers = server.received[0]["headers"]
        assert HEADER_IDEMPOTENCY_KEY not in headers


class TestRetryPolicy:
    def test_retries_429_then_succeeds(self, gateway_config):
        server = gateway_config["_server"]
        server.enqueue(429)
        server.enqueue(200, {"trace_id": "ok"})
        client = GatewayClient(gateway_config)
        r = client.ingest({"x": 1}, source_id="s", content_type="t/t")
        assert r.status_code == 200
        assert r.attempts == 2

    def test_retries_503_then_succeeds(self, gateway_config):
        server = gateway_config["_server"]
        server.enqueue(503)
        server.enqueue(503)
        server.enqueue(200, {"trace_id": "ok"})
        gateway_config["max_retries"] = 3
        client = GatewayClient(gateway_config)
        r = client.ingest({"x": 1}, source_id="s", content_type="t/t")
        assert r.status_code == 200
        assert r.attempts == 3

    def test_retries_exhausted_returns_last_response(self, gateway_config):
        server = gateway_config["_server"]
        for _ in range(5):
            server.enqueue(500, "boom")
        gateway_config["max_retries"] = 2
        client = GatewayClient(gateway_config)
        r = client.ingest({"x": 1}, source_id="s", content_type="t/t")
        # max_retries=2 means up to 3 attempts total
        assert r.status_code == 500
        assert r.attempts == 3
        assert r.raw_text == "boom"

    def test_4xx_other_than_429_not_retried(self, gateway_config):
        server = gateway_config["_server"]
        server.enqueue(400, {"error": "bad request"})
        server.enqueue(200, {"trace_id": "x"})  # would only be used if we retried
        client = GatewayClient(gateway_config)
        r = client.ingest({"x": 1}, source_id="s", content_type="t/t")
        assert r.status_code == 400
        assert r.attempts == 1

    def test_backoff_progression(self, gateway_config):
        """Backoff should be base * 2^(attempt-1)."""
        sleeps: list[float] = []
        gateway_config["backoff_base_seconds"] = 0.5
        gateway_config["max_retries"] = 3
        server = gateway_config["_server"]
        server.enqueue(429)
        server.enqueue(429)
        server.enqueue(200, {"trace_id": "x"})
        client = GatewayClient(gateway_config, sleep=sleeps.append)
        client.ingest({"x": 1}, source_id="s", content_type="t/t")
        assert sleeps == [0.5, 1.0]


class TestHealth:
    def test_health_200(self, gateway_config):
        gateway_config["_server"].health_status = 200
        assert GatewayClient(gateway_config).health() is True

    def test_health_503(self, gateway_config):
        gateway_config["_server"].health_status = 503
        assert GatewayClient(gateway_config).health() is False

    def test_health_unreachable(self):
        client = GatewayClient({
            "gateway_url": "http://127.0.0.1:1",
            "api_key": "k",
            "timeout_seconds": 1,
        })
        assert client.health() is False
