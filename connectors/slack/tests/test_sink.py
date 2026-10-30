"""SlackMessageSink: routing (message, ephemeral, file), attribution by
identity, the send gate, channel names, failures as PushResults, and that
the token never leaves the Authorization header."""

from __future__ import annotations

import io
from typing import Any

import pytest
from aisquare.pipe import DataEnvelope, PushParams, PushResult, get_connector

from aisquare_pipe_slack import SlackMessageSink, connector, settings_from_config
from aisquare_pipe_slack.catalog import TOOLS
from aisquare_pipe_slack.toolkit import gate

from .fakes import (
    BOT_CONFIG,
    BOT_TOKEN,
    USER_CONFIG,
    USER_TOKEN,
    FakeSession,
    channels_page,
    ok,
    response,
    slack_error,
)

C = "C0123456"
TS = "1761400000.000200"
MARK = "Sent by pipe"
UPLOAD_URL = "https://files.slack.com/upload/v1/ABC123"


def envelope(data: Any, content_type: str = "text/plain", **metadata: Any) -> DataEnvelope:
    return DataEnvelope(content_type=content_type, data=data, source_id="test", metadata=metadata)


def push(
    env: DataEnvelope, *replies: Any, config: dict[str, Any] | None = None, **params: Any
) -> tuple[PushResult, FakeSession, list[float]]:
    session = FakeSession(list(replies))
    sleeps: list[float] = []
    sink = SlackMessageSink(session=session, sleep=sleeps.append)  # type: ignore[arg-type]
    result = sink.push(env, dict(USER_CONFIG if config is None else config), PushParams({"channel": C, **params}))
    return result, session, sleeps


class TestRegistration:
    def test_registered_as_the_slack_message_sink(self):
        assert get_connector("slack-message-sink") is SlackMessageSink

    def test_accepts_anything(self):
        sink = SlackMessageSink()
        assert sink.accepts(envelope("hi")) and sink.accepts(envelope(b"%PDF", "application/pdf"))
        assert sink.max_size() == connector.UPLOAD_MAX_BYTES

    @pytest.mark.parametrize(
        "config, valid",
        [
            ({}, False),
            ({"access_token": ""}, False),
            ({"access_token": "xoxp-1 2"}, False),
            ({"access_token": 5}, False),
            ({"access_token": USER_TOKEN, "identity": "app"}, False),
            ({"access_token": USER_TOKEN, "enabled_tiers": ["*"]}, False),
            (USER_CONFIG, True),
            (BOT_CONFIG, True),
            ({"access_token": f"  {USER_TOKEN}\n"}, True),
        ],
    )
    def test_validate_config_is_an_offline_shape_check(self, config, valid):
        assert SlackMessageSink().validate_config(config) is valid


class TestMessages:
    def test_plain_text_as_the_user_carries_the_marker(self):
        result, session, _ = push(envelope("Deploy done"), ok(channel=C, ts=TS))
        assert result.success, result.error
        assert (result.ref, result.metadata) == (TS, {"kind": "message", "channel": C, "ephemeral": False})
        call = session.last
        assert call.api_method == "chat.postMessage" and call.is_json
        assert call.args == {"channel": C, "text": f"Deploy done\n_{MARK}_"}
        assert call.headers["Authorization"] == f"Bearer {USER_TOKEN}"

    def test_markdown_goes_as_markdown_text(self):
        result, session, _ = push(envelope("**Build green**", "text/markdown; charset=utf-8"), ok(ts=TS))
        assert result.success, result.error
        assert session.last.args == {"channel": C, "markdown_text": f"**Build green**\n\n_{MARK}_"}

    def test_the_bot_posts_without_a_marker_unless_asked(self):
        result, session, _ = push(envelope("hi"), ok(ts=TS), config=BOT_CONFIG)
        assert result.success, result.error
        assert session.last.args == {"channel": C, "text": "hi"}
        assert session.last.headers["Authorization"] == f"Bearer {BOT_TOKEN}"
        _, session, _ = push(envelope("hi"), ok(ts=TS), config={**BOT_CONFIG, "bot_attribution": True,
                                                                 "attribution_name": "aisquare"})
        assert session.last.args == {"channel": C, "text": "hi\n_Sent by aisquare_"}

    def test_json_with_blocks_gets_a_context_block(self):
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*Hi*"}}]
        result, session, _ = push(envelope({"text": "Hi", "blocks": blocks}, "application/json"), ok(ts=TS))
        assert result.success, result.error
        sent = session.last.args
        assert sent["text"] == "Hi"
        assert sent["blocks"][:-1] == blocks
        assert sent["blocks"][-1] == {"type": "context", "elements": [{"type": "mrkdwn", "text": MARK}]}

    def test_metadata_blocks_override_the_envelopes(self):
        mine = [{"type": "divider"}]
        env = envelope({"text": "Hi", "blocks": [{"type": "header"}]}, "application/json", blocks=mine)
        _, session, _ = push(env, ok(ts=TS), config=BOT_CONFIG)
        assert session.last.args["blocks"] == mine

    def test_json_markdown_text(self):
        _, session, _ = push(envelope({"markdown_text": "_hi_"}, "application/json"), ok(ts=TS), config=BOT_CONFIG)
        assert session.last.args == {"channel": C, "markdown_text": "_hi_"}

    def test_thread_and_options_pass_through(self):
        _, session, _ = push(
            envelope("hi"), ok(ts=TS), config=BOT_CONFIG,
            thread_ts="1761312000.004100", reply_broadcast=True, unfurl_links=False,
        )
        assert session.last.args == {
            "channel": C, "thread_ts": "1761312000.004100", "reply_broadcast": True, "unfurl_links": False, "text": "hi",
        }

    def test_ephemeral(self):
        result, session, _ = push(envelope("psst"), ok(message_ts=TS), ephemeral_user="U0123456")
        assert result.success, result.error
        assert (result.ref, result.metadata["ephemeral"]) == (TS, True)
        assert session.last.api_method == "chat.postEphemeral"
        assert session.last.args == {"channel": C, "user": "U0123456", "text": f"psst\n_{MARK}_"}

    def test_a_channel_name_is_looked_up(self):
        replies = (channels_page({"id": "C0GENERAL1", "name": "general"}), ok(channel="C0GENERAL1", ts=TS))
        result, session, _ = push(envelope("hi"), *replies, channel="#general")
        assert result.success, result.error
        assert session.methods == ["conversations.list", "chat.postMessage"]
        assert session.last.args["channel"] == "C0GENERAL1"

    @pytest.mark.parametrize(
        "env, message",
        [
            (envelope(""), "message content is missing"),
            (envelope({"title": "x"}, "application/json"), "must carry text, markdown_text or blocks"),
            (envelope({"markdown_text": "a", "blocks": [{"type": "divider"}]}, "application/json"),
             "markdown_text cannot be combined"),
            (envelope("hi", blocks="not json"), "blocks is not valid JSON"),
        ],
    )
    def test_bad_content_fails_before_any_request(self, env, message):
        result, session, _ = push(env)
        assert result.success is False
        assert message in (result.error or "")
        assert result.metadata["error_code"] == "invalid_arguments"
        assert session.calls == []


class TestFiles:
    def _replies(self, file_id: str = "F0FILE1234") -> list[Any]:
        return [
            ok(upload_url=UPLOAD_URL, file_id=file_id),
            response(200, content=b"OK - 5"),
            ok(files=[{"id": file_id, "name": "report.csv", "permalink": "https://acme.slack.com/files/F0FILE1234"}]),
        ]

    def test_bytes_are_uploaded_into_the_channel(self):
        env = envelope(b"a,b\n1,2\n", "text/csv", filename="report.csv", title="Report")
        result, session, _ = push(env, *self._replies(), initial_comment="Weekly numbers")
        assert result.success, result.error
        assert result.ref == "F0FILE1234"
        assert result.metadata == {
            "kind": "file", "channel": C, "file_id": "F0FILE1234", "name": "report.csv", "size": 8,
            "permalink": "https://acme.slack.com/files/F0FILE1234",
        }
        ticket, upload, complete = session.calls
        assert ticket.api_method == "files.getUploadURLExternal"
        assert ticket.args == {"filename": "report.csv", "length": "8"}
        assert (upload.url, upload.data) == (UPLOAD_URL, b"a,b\n1,2\n")
        assert "Authorization" not in upload.headers  # pre-signed: never the token
        assert complete.api_method == "files.completeUploadExternal" and complete.is_json
        assert complete.args == {
            "files": [{"id": "F0FILE1234", "title": "Report"}],
            "channels": C,
            "initial_comment": f"Weekly numbers\n_{MARK}_",
        }

    def test_a_bot_file_has_no_comment_unless_given(self):
        env = envelope(b"%PDF-1.7", "application/pdf")
        result, session, _ = push(env, *self._replies(), config=BOT_CONFIG, thread_ts="1761312000.004100")
        assert result.success, result.error
        assert session.calls[0].args == {"filename": "file.pdf", "length": "8"}
        assert session.last.args == {"files": [{"id": "F0FILE1234"}], "channels": C, "thread_ts": "1761312000.004100"}

    def test_the_users_file_gets_the_marker_as_its_comment(self):
        _, session, _ = push(envelope(b"x", "application/octet-stream"), *self._replies())
        assert session.last.args["initial_comment"] == f"_{MARK}_"

    @pytest.mark.parametrize(
        "env, sent",
        [
            (envelope("plain words", "text/csv"), b"plain words"),
            (envelope({"a": 1}, "application/vnd.acme+json"), b'{"a": 1}'),
            # JSON text is a document, not a message: only a dict is one.
            (envelope('{"a": 1}', "application/json"), b'{"a": 1}'),
            # A dict is a message only as application/json.
            (envelope({"text": "hi"}, "text/plain"), b'{"text": "hi"}'),
            (DataEnvelope(content_type="text/plain", data="", source_id="t", stream=io.BytesIO(b"streamed")), b"streamed"),
        ],
        ids=["str-non-message-type", "dict-non-message-type", "str-json", "dict-text-plain", "stream"],
    )
    def test_what_becomes_a_file(self, env, sent):
        result, session, _ = push(env, *self._replies(), config=BOT_CONFIG)
        assert result.success, result.error
        assert session.calls[1].data == sent

    def test_over_the_cap_fails_before_any_request(self, monkeypatch):
        monkeypatch.setattr(connector, "UPLOAD_MAX_BYTES", 4)
        for env in (envelope(b"12345", "application/pdf"),
                    DataEnvelope(content_type="application/pdf", data=b"", source_id="t", stream=io.BytesIO(b"12345"))):
            result, session, _ = push(env)
            assert result.success is False and "upload cap" in (result.error or "")
            assert session.calls == []

    @pytest.mark.parametrize(
        "env, params, message",
        [
            (envelope(b"", "application/pdf"), {}, "nothing to upload"),
            (envelope(b"x", "application/pdf"), {"ephemeral_user": "U0123456"}, "can't be sent as an ephemeral"),
        ],
    )
    def test_refused_uploads(self, env, params, message):
        result, session, _ = push(env, **params)
        assert result.success is False and message in (result.error or "")
        assert session.calls == []

    def test_a_failed_upload_step_is_reported(self):
        result, session, _ = push(envelope(b"x", "application/pdf"), ok(upload_url=UPLOAD_URL, file_id="F1"),
                                  response(500, content=b"boom"))
        assert result.success is False
        assert result.error == "files.upload: http_500 — Slack had a problem; try again later"
        assert result.metadata["error_code"] == "upstream_error"
        assert len(session.calls) == 2  # never completed


class TestGateAndFailures:
    def test_without_a_channel_or_token_nothing_is_sent(self):
        session = FakeSession()
        sink = SlackMessageSink(session=session)  # type: ignore[arg-type]
        no_channel = sink.push(envelope("hi"), dict(USER_CONFIG), PushParams({}))
        no_token = sink.push(envelope("hi"), {}, PushParams({"channel": C}))
        no_params = sink.push(envelope("hi"), dict(USER_CONFIG))
        assert no_channel.metadata["error_code"] == no_params.metadata["error_code"] == "invalid_arguments"
        assert "params['channel'] is required" in (no_channel.error or "")
        assert no_token.metadata["error_code"] == "forbidden"
        assert session.calls == []

    @pytest.mark.parametrize(
        "params, message",
        [
            ({"thread_ts": 1761312000.0041}, "params['thread_ts'] must be a non-empty string"),
            ({"thread_ts": "  "}, "params['thread_ts'] must be a non-empty string"),
            ({"ephemeral_user": {"x": 1}}, "params['ephemeral_user'] must be a non-empty string"),
            ({"reply_broadcast": "yes"}, "params['reply_broadcast'] must be true or false"),
            ({"unfurl_links": 1}, "params['unfurl_links'] must be true or false"),
            ({"unfurl_media": "false"}, "params['unfurl_media'] must be true or false"),
            ({"filename": 5}, "params['filename'] must be a non-empty string"),
            ({"channel": ["C0123456"]}, "params['channel'] must be a non-empty string"),
        ],
    )
    def test_params_are_checked_before_any_request(self, params, message):
        # A float thread_ts would go out as a JSON number and lose digits.
        result, session, _ = push(envelope("hi"), **params)
        assert result.success is False
        assert result.error == message
        assert result.metadata["error_code"] == "invalid_arguments"
        assert session.calls == []

    def test_a_config_error_names_its_key(self):
        result, session, _ = push(envelope("hi"), config={**USER_CONFIG, "identity": "robot"})
        assert result.metadata["error_code"] == "forbidden"
        assert "config['identity'] must be 'user' or 'bot'" in (result.error or "")
        assert session.calls == []

    def test_send_must_be_enabled(self):
        result, session, _ = push(envelope("hi"), config={**USER_CONFIG, "enabled_tiers": ["read"]})
        assert result.success is False
        assert result.metadata["error_code"] == "forbidden"
        assert "config['enabled_tiers']" in (result.error or "")
        assert "'SLACK_SEND_MESSAGE' to config['enabled_tools']" in (result.error or "")
        assert session.calls == []

    @pytest.mark.parametrize(
        "content, slug, extra",
        [
            ("hi", "SLACK_SEND_MESSAGE", {}),
            ("hi", "SLACK_SEND_EPHEMERAL_MESSAGE", {"ephemeral_user": "U0123456"}),
            (b"%PDF", "SLACK_UPLOAD_OR_CREATE_A_FILE_IN_SLACK", {}),
        ],
    )
    def test_the_sink_uses_the_toolkits_own_gate(self, content, slug, extra):
        # mn6: one gate, so the sink can't drift from the tools' tiers/identities.
        config = {**USER_CONFIG, "enabled_tiers": ["read"]}
        env = envelope(content, "application/pdf") if isinstance(content, bytes) else envelope(content)
        result, session, _ = push(env, config=config, **extra)
        expected = gate(TOOLS[slug], settings_from_config(config))
        assert expected is not None and result.error == expected.error
        assert session.calls == []

    def test_one_slug_enables_its_kind_of_push(self):
        config = {**USER_CONFIG, "enabled_tiers": ["read"], "enabled_tools": ["SLACK_SEND_EPHEMERAL_MESSAGE"]}
        assert push(envelope("hi"), ok(message_ts=TS), config=config, ephemeral_user="U0123456")[0].success
        refused, session, _ = push(envelope("hi"), config=config)
        assert refused.metadata["error_code"] == "forbidden" and session.calls == []
        upload = push(envelope(b"x", "application/pdf"), config=config)[0]
        assert "'SLACK_UPLOAD_OR_CREATE_A_FILE_IN_SLACK'" in (upload.error or "")

    def test_slack_errors_keep_their_code_and_hint(self):
        result, _, _ = push(envelope("hi"), slack_error("not_in_channel"), config=BOT_CONFIG)
        assert result.success is False
        assert result.error is not None and result.error.startswith("chat.postMessage: not_in_channel")
        assert result.metadata == {"error_code": "forbidden", "slack_error": "not_in_channel", "status": 200}

    def test_a_rate_limit_is_retried_then_reported(self):
        limited = [response(429, {"ok": False, "error": "ratelimited"}, headers={"Retry-After": "3"}) for _ in range(3)]
        result, session, sleeps = push(envelope("hi"), *limited)
        assert result.success is False
        assert result.metadata["error_code"] == "rate_limited" and result.metadata["retry_after"] == 3.0
        assert sleeps == [3.0, 3.0] and len(session.calls) == 3

    def test_server_errors_are_not_retried_for_a_post(self):
        # Retrying a chat.postMessage that failed with 5xx could post twice.
        result, session, sleeps = push(envelope("hi"), response(503, {"ok": False, "error": "service_unavailable"}))
        assert result.metadata["error_code"] == "upstream_error"
        assert sleeps == [] and len(session.calls) == 1

    def test_unexpected_errors_never_raise_or_leak(self):
        session = FakeSession([RuntimeError(f"boom {USER_TOKEN}")])
        result = SlackMessageSink(session=session).push(  # type: ignore[arg-type]
            envelope("hi"), dict(USER_CONFIG), PushParams({"channel": C})
        )
        assert result.success is False
        assert result.error == "slack: push failed (RuntimeError)"
        assert USER_TOKEN not in repr(result)

    def test_the_token_is_never_in_a_failure(self):
        for reply in (slack_error("invalid_auth"), response(500, content=USER_TOKEN.encode())):
            result, session, _ = push(envelope("hi"), reply)
            assert USER_TOKEN not in repr(result)
            assert all(USER_TOKEN not in str(c.data) for c in session.calls)
