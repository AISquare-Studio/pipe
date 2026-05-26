"""Tests for AISquareGatewaySink."""

from __future__ import annotations

import pytest

from aisquare.pipe.core.envelope import DataEnvelope, PushResult

from aisquare_pipe_gateway.connector import AISquareGatewaySink

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

    def test_envelope_data_is_forwarded_verbatim(self, gateway_config):
        """Sink must forward envelope.data (a TraceBatch dict) as the JSON body."""
        server = gateway_config["_server"]
        server.enqueue(200, {"trace_id": "x"})
        sink = AISquareGatewaySink()
        env = make_trace_envelope(trace_id="n8n-wf-1-9", execution_id="9")
        sink.push(env, gateway_config)

        body = server.received[0]["body"]
        # The body is exactly the envelope's data — a TraceBatch.
        assert body["trace_id"] == "n8n-wf-1-9"
        assert isinstance(body["spans"], list)
        assert body["spans"][0]["span_id"].startswith("n8n-wf-1-9")
        # source_id / content_type ride as side headers, not body fields.
        assert "source_id" not in body
        assert "metadata" not in body

    def test_source_id_and_content_type_in_headers(self, gateway_config):
        server = gateway_config["_server"]
        server.enqueue(200, {"trace_id": "x"})
        sink = AISquareGatewaySink()
        sink.push(make_trace_envelope(), gateway_config)

        headers = server.received[0]["headers"]
        # http.server normalises header capitalisation in different ways across
        # Python versions, so look case-insensitively.
        normalised = {k.lower(): v for k, v in headers.items()}
        assert normalised["x-aisquare-source-id"] == "n8n"
        assert normalised["x-aisquare-content-type"] == "application/x-aisquare-trace+json"
        assert normalised["x-api-key"] == "test-gateway-key"

    def test_idempotency_key_lifted_from_metadata(self, gateway_config):
        server = gateway_config["_server"]
        server.enqueue(200, {"trace_id": "x"})
        sink = AISquareGatewaySink()
        env = make_trace_envelope(idempotency_key="n8n:final:n8n-wf-1-1")
        sink.push(env, gateway_config)

        normalised = {k.lower(): v for k, v in server.received[0]["headers"].items()}
        assert normalised["x-idempotency-key"] == "n8n:final:n8n-wf-1-1"

    def test_no_idempotency_header_when_absent(self, gateway_config):
        server = gateway_config["_server"]
        server.enqueue(200, {"trace_id": "x"})
        sink = AISquareGatewaySink()
        sink.push(make_trace_envelope(), gateway_config)

        normalised = {k.lower(): v for k, v in server.received[0]["headers"].items()}
        assert "x-idempotency-key" not in normalised

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
        envelope = make_trace_envelope()
        result = sink.push(envelope, {})
        assert isinstance(result, PushResult)
        assert result.success is False

    def test_non_dict_data_returns_failure(self, gateway_config):
        gateway_config["_server"].enqueue(200, {"trace_id": "x"})
        sink = AISquareGatewaySink()
        envelope = DataEnvelope(
            content_type="application/x-aisquare-trace+json",
            data="hello",  # not a dict — sink should reject
            source_id="t",
        )
        result = sink.push(envelope, gateway_config)
        assert result.success is False
        assert "TraceBatch" in result.error or "envelope.data" in result.error

    def test_missing_spans_returns_failure(self, gateway_config):
        gateway_config["_server"].enqueue(200, {"trace_id": "x"})
        sink = AISquareGatewaySink()
        envelope = DataEnvelope(
            content_type="application/x-aisquare-trace+json",
            data={"trace_id": "x"},  # missing 'spans'
            source_id="t",
        )
        result = sink.push(envelope, gateway_config)
        assert result.success is False


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
            data={"trace_id": "t", "spans": []},
            source_id="t",
        )
        assert sink.accepts(envelope) is True

    def test_rejects_other_types(self):
        sink = AISquareGatewaySink()
        envelope = DataEnvelope(
            content_type="text/plain", data="x", source_id="t"
        )
        assert sink.accepts(envelope) is False
