"""Tests for AISquareGatewaySink."""

from __future__ import annotations

from aisquare.pipe.core.envelope import DataEnvelope, PushResult

from aisquare_pipe_gateway.sink import AISquareGatewaySink

from tests.helpers import make_trace_envelope


class TestSinkPush:
    def test_2xx_returns_success(self, gateway_config):
        gateway_config["_server"].enqueue(200, {"trace_id": "trace-42"})
        sink = AISquareGatewaySink()
        result = sink.push(make_trace_envelope(), gateway_config)
        assert isinstance(result, PushResult)
        assert result.success is True
        assert result.ref == "trace-42"
        assert result.metadata["http_status"] == 200

    def test_envelope_payload_is_wrapped(self, gateway_config):
        server = gateway_config["_server"]
        server.enqueue(200, {"trace_id": "x"})
        sink = AISquareGatewaySink()
        env = make_trace_envelope(event="workflow_start", execution_id="9")
        sink.push(env, gateway_config)
        body = server.received[0]["body"]
        assert body["source_id"] == "n8n"
        assert body["content_type"] == "application/x-aisquare-trace+json"
        assert body["data"]["event"] == "workflow_start"
        assert body["metadata"]["n8n_execution_id"] == "9"

    def test_4xx_returns_failure(self, gateway_config):
        gateway_config["_server"].enqueue(400, {"error": "bad"})
        sink = AISquareGatewaySink()
        result = sink.push(make_trace_envelope(), gateway_config)
        assert result.success is False
        assert "400" in result.error
        assert result.metadata["http_status"] == 400

    def test_retry_then_success(self, gateway_config):
        server = gateway_config["_server"]
        server.enqueue(429)
        server.enqueue(200, {"trace_id": "ok"})
        sink = AISquareGatewaySink()
        result = sink.push(make_trace_envelope(), gateway_config)
        assert result.success is True
        assert result.metadata["attempts"] == 2

    def test_retries_exhausted_returns_failure(self, gateway_config):
        server = gateway_config["_server"]
        gateway_config["max_retries"] = 1
        server.enqueue(500)
        server.enqueue(500)
        sink = AISquareGatewaySink()
        result = sink.push(make_trace_envelope(), gateway_config)
        assert result.success is False
        assert result.metadata["http_status"] == 500
        assert result.metadata["attempts"] == 2

    def test_transport_error_returns_failure(self):
        sink = AISquareGatewaySink()
        config = {
            "gateway_url": "http://127.0.0.1:1",
            "api_key": "k",
            "max_retries": 0,
            "timeout_seconds": 1,
            "backoff_base_seconds": 0.0,
        }
        result = sink.push(make_trace_envelope(), config)
        assert result.success is False
        assert "Transport error" in result.error

    def test_invalid_config_returns_failure(self):
        """Compliance suite calls push() with an empty config."""
        sink = AISquareGatewaySink()
        envelope = DataEnvelope(
            content_type="text/plain", data="x", source_id="t"
        )
        result = sink.push(envelope, {})
        assert isinstance(result, PushResult)
        assert result.success is False

    def test_string_data_is_wrapped(self, gateway_config):
        server = gateway_config["_server"]
        server.enqueue(200, {"trace_id": "x"})
        sink = AISquareGatewaySink()
        envelope = DataEnvelope(
            content_type="text/plain", data="hello", source_id="t", metadata={}
        )
        sink.push(envelope, gateway_config)
        body = server.received[0]["body"]
        assert body["data"] == "hello"


class TestSinkValidateConfig:
    def test_missing_config(self):
        assert AISquareGatewaySink().validate_config({}) is False

    def test_health_200(self, gateway_config):
        gateway_config["_server"].health_status = 200
        assert AISquareGatewaySink().validate_config(gateway_config) is True

    def test_health_503(self, gateway_config):
        gateway_config["_server"].health_status = 503
        assert AISquareGatewaySink().validate_config(gateway_config) is False


class TestSinkAttributes:
    def test_input_types(self):
        sink = AISquareGatewaySink()
        assert sink.input_types == ["application/x-aisquare-trace+json"]

    def test_accepts_trace_type(self):
        sink = AISquareGatewaySink()
        envelope = DataEnvelope(
            content_type="application/x-aisquare-trace+json",
            data={"event": "x"},
            source_id="t",
        )
        assert sink.accepts(envelope) is True

    def test_rejects_other_types(self):
        sink = AISquareGatewaySink()
        envelope = DataEnvelope(
            content_type="text/plain", data="x", source_id="t"
        )
        assert sink.accepts(envelope) is False
