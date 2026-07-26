"""Tests for the Slack client, sink, and webhook helpers (no network)."""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import MagicMock

import pytest

from aisquare.pipe.core.envelope import DataEnvelope, PushParams
from aisquare_pipe_slack import webhook
from aisquare_pipe_slack.client import (
    SlackAuthError,
    SlackClient,
    SlackError,
    SlackRateLimited,
)
from aisquare_pipe_slack.connector import SlackMessageSink


@pytest.fixture
def config() -> dict:
    return {"bot_token": "xoxb-test-token"}


def _response(status_code: int = 200, json_data: dict | None = None, headers: dict | None = None):
    response = MagicMock()
    response.status_code = status_code
    response.headers = headers or {}
    response.text = json.dumps(json_data) if json_data is not None else ""
    if json_data is None:
        response.json.side_effect = ValueError("no json")
    else:
        response.json.return_value = json_data
    return response


def _envelope(data, content_type: str = "application/json", metadata: dict | None = None):
    return DataEnvelope(
        content_type=content_type,
        data=data,
        source_id="test",
        metadata=metadata or {},
    )


class TestClient:
    def test_post_message_hits_api_with_bearer_token(self, config):
        session = MagicMock()
        session.post.return_value = _response(
            200, {"ok": True, "channel": "C123", "ts": "1700000000.000100"}
        )
        payload = SlackClient(config, session=session).post_message("C123", text="hi")

        assert payload["ts"] == "1700000000.000100"
        url = session.post.call_args[0][0]
        kwargs = session.post.call_args[1]
        assert url.endswith("/chat.postMessage")
        assert kwargs["headers"]["Authorization"] == "Bearer xoxb-test-token"
        assert kwargs["json"] == {"channel": "C123", "text": "hi"}

    def test_empty_optional_fields_are_stripped(self, config):
        session = MagicMock()
        session.post.return_value = _response(200, {"ok": True, "ts": "1.2"})
        SlackClient(config, session=session).post_message(
            "C123", text="hi", blocks=[], thread_ts="", username="", icon_emoji=""
        )
        assert session.post.call_args[1]["json"] == {"channel": "C123", "text": "hi"}

    def test_post_ephemeral_targets_user(self, config):
        session = MagicMock()
        session.post.return_value = _response(200, {"ok": True, "message_ts": "1.3"})
        payload = SlackClient(config, session=session).post_ephemeral(
            "C123", user="U777", text="only you"
        )
        assert payload["message_ts"] == "1.3"
        assert session.post.call_args[0][0].endswith("/chat.postEphemeral")
        assert session.post.call_args[1]["json"]["user"] == "U777"

    def test_ok_false_auth_error_maps_to_auth_error(self, config):
        session = MagicMock()
        session.post.return_value = _response(200, {"ok": False, "error": "invalid_auth"})
        with pytest.raises(SlackAuthError) as exc_info:
            SlackClient(config, session=session).post_message("C123", text="hi")
        assert exc_info.value.error == "invalid_auth"

    def test_ok_false_missing_scope_is_auth_error(self, config):
        session = MagicMock()
        session.post.return_value = _response(200, {"ok": False, "error": "missing_scope"})
        with pytest.raises(SlackAuthError):
            SlackClient(config, session=session).post_message("C123", text="hi")

    def test_ok_false_domain_error_maps_to_slack_error(self, config):
        session = MagicMock()
        session.post.return_value = _response(200, {"ok": False, "error": "channel_not_found"})
        with pytest.raises(SlackError) as exc_info:
            SlackClient(config, session=session).post_message("CBAD", text="hi")
        assert exc_info.value.error == "channel_not_found"
        assert not isinstance(exc_info.value, SlackAuthError)

    def test_http_429_maps_to_rate_limited_with_retry_after(self, config):
        session = MagicMock()
        session.post.return_value = _response(429, {"ok": False}, headers={"Retry-After": "17"})
        with pytest.raises(SlackRateLimited) as exc_info:
            SlackClient(config, session=session).post_message("C123", text="hi")
        assert exc_info.value.retry_after == 17

    def test_ok_false_ratelimited_maps_to_rate_limited(self, config):
        session = MagicMock()
        session.post.return_value = _response(200, {"ok": False, "error": "ratelimited"})
        with pytest.raises(SlackRateLimited):
            SlackClient(config, session=session).post_message("C123", text="hi")

    def test_http_401_maps_to_auth_error(self, config):
        session = MagicMock()
        session.post.return_value = _response(401, {"ok": False})
        with pytest.raises(SlackAuthError):
            SlackClient(config, session=session).post_message("C123", text="hi")

    def test_non_json_body_maps_to_slack_error(self, config):
        session = MagicMock()
        session.post.return_value = _response(200, None)
        with pytest.raises(SlackError) as exc_info:
            SlackClient(config, session=session).auth_test()
        assert exc_info.value.error == "invalid_json"

    def test_validate_checks_token_presence(self):
        assert SlackClient({"bot_token": "xoxb-x"}).validate() is True
        assert SlackClient({}).validate() is False


class TestSink:
    def test_missing_channel_fails_before_any_client_work(self, config):
        result = SlackMessageSink().push(_envelope("hello"), config)
        assert result.success is False
        assert "channel" in result.error

    def test_missing_token_fails_without_network(self):
        result = SlackMessageSink().push(
            _envelope("hello"), {}, PushParams({"channel": "C123"})
        )
        assert result.success is False
        assert "bot_token" in result.error

    def test_empty_payload_fails(self, config):
        result = SlackMessageSink().push(
            _envelope({"text": ""}), config, PushParams({"channel": "C123"})
        )
        assert result.success is False
        assert "neither text nor blocks" in result.error

    def test_unsupported_data_type_fails(self, config):
        result = SlackMessageSink().push(
            _envelope(b"bytes", content_type="application/octet-stream"),
            config,
            PushParams({"channel": "C123"}),
        )
        assert result.success is False

    def test_str_envelope_posts_channel_message(self, config, monkeypatch):
        calls = {}

        def fake_post_message(self, channel, text="", blocks=None, thread_ts="", **kw):
            calls.update(channel=channel, text=text, blocks=blocks, thread_ts=thread_ts)
            return {"ok": True, "channel": channel, "ts": "1700.42"}

        monkeypatch.setattr(SlackClient, "post_message", fake_post_message)
        result = SlackMessageSink().push(
            _envelope("hello team", content_type="text/plain"),
            config,
            PushParams({"channel": "C123", "thread_ts": "1699.1"}),
        )
        assert result.success is True
        assert result.ref == "1700.42"
        assert result.metadata == {"channel": "C123", "ephemeral": False}
        assert calls["text"] == "hello team"
        assert calls["thread_ts"] == "1699.1"

    def test_dict_envelope_carries_blocks(self, config, monkeypatch):
        calls = {}

        def fake_post_message(self, channel, text="", blocks=None, **kw):
            calls.update(text=text, blocks=blocks)
            return {"ok": True, "ts": "1.1"}

        monkeypatch.setattr(SlackClient, "post_message", fake_post_message)
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*hi*"}}]
        result = SlackMessageSink().push(
            _envelope({"text": "hi", "blocks": blocks}),
            config,
            PushParams({"channel": "C123"}),
        )
        assert result.success is True
        assert calls["blocks"] == blocks

    def test_metadata_blocks_override_envelope_blocks(self, config, monkeypatch):
        calls = {}

        def fake_post_message(self, channel, text="", blocks=None, **kw):
            calls.update(blocks=blocks)
            return {"ok": True, "ts": "1.1"}

        monkeypatch.setattr(SlackClient, "post_message", fake_post_message)
        meta_blocks = [{"type": "divider"}]
        SlackMessageSink().push(
            _envelope({"text": "hi", "blocks": [{"type": "section"}]}, metadata={"blocks": meta_blocks}),
            config,
            PushParams({"channel": "C123"}),
        )
        assert calls["blocks"] == meta_blocks

    def test_ephemeral_user_routes_to_post_ephemeral(self, config, monkeypatch):
        calls = {}

        def fake_post_ephemeral(self, channel, user, text="", blocks=None, thread_ts=""):
            calls.update(channel=channel, user=user)
            return {"ok": True, "message_ts": "2.2"}

        monkeypatch.setattr(SlackClient, "post_ephemeral", fake_post_ephemeral)
        result = SlackMessageSink().push(
            _envelope("psst"),
            config,
            PushParams({"channel": "C123", "ephemeral_user": "U777"}),
        )
        assert result.success is True
        assert result.ref == "2.2"
        assert result.metadata["ephemeral"] is True
        assert calls["user"] == "U777"

    def test_client_error_becomes_failed_result(self, config, monkeypatch):
        def fake_post_message(self, *a, **kw):
            raise SlackError(200, "channel_not_found")

        monkeypatch.setattr(SlackClient, "post_message", fake_post_message)
        result = SlackMessageSink().push(
            _envelope("hello"), config, PushParams({"channel": "CBAD"})
        )
        assert result.success is False
        assert "channel_not_found" in result.error

    def test_validate_config(self, config):
        sink = SlackMessageSink()
        assert sink.validate_config(config) is True
        assert sink.validate_config({}) is False


class TestWebhook:
    SECRET = "8f742231b10e8888abcd99yyyzzz85a5"

    def _sign(self, body: bytes, ts: str, secret: str | None = None) -> str:
        base = b"v0:" + ts.encode() + b":" + body
        return "v0=" + hmac.new((secret or self.SECRET).encode(), base, hashlib.sha256).hexdigest()

    def test_valid_signature_verifies(self):
        body = b'{"type":"event_callback","event_id":"Ev123"}'
        ts = "1531420618"
        assert webhook.verify_slack_signature(body, ts, self._sign(body, ts), self.SECRET)

    def test_tampered_body_fails(self):
        body = b'{"type":"event_callback"}'
        ts = "1531420618"
        signature = self._sign(body, ts)
        assert not webhook.verify_slack_signature(body + b"x", ts, signature, self.SECRET)

    def test_wrong_secret_fails(self):
        body = b"{}"
        ts = "1531420618"
        signature = self._sign(body, ts, secret="other-secret")
        assert not webhook.verify_slack_signature(body, ts, signature, self.SECRET)

    def test_missing_parts_fail_closed(self):
        assert not webhook.verify_slack_signature(b"{}", "", "v0=abc", self.SECRET)
        assert not webhook.verify_slack_signature(b"{}", "1", "", self.SECRET)
        assert not webhook.verify_slack_signature(b"{}", "1", "v0=abc", "")

    def test_timestamp_window(self):
        assert webhook.timestamp_in_window("1000", now=1200.0)
        assert not webhook.timestamp_in_window("1000", now=1400.0)
        assert not webhook.timestamp_in_window("not-a-number", now=1000.0)
        assert not webhook.timestamp_in_window("", now=1000.0)
