"""Unit tests for ComposioSink.push: tool resolution, argument layering,
file uploads, and error behaviour."""

from __future__ import annotations

import io
import json

from aisquare.pipe.core.envelope import DataEnvelope, PushParams

from aisquare.pipe.errors import PipelineError

from aisquare_pipe_composio.connector import ComposioSink


def _envelope(data, content_type="application/json", metadata=None, stream=None):
    return DataEnvelope(
        content_type=content_type,
        data=data,
        source_id="test-source",
        metadata=metadata or {},
        stream=stream,
    )


def _push(sink, envelope, config, **params):
    return sink.push(envelope, config, PushParams(params=params))


class TestToolResolution:
    def test_missing_tool_fails(self, mock_client, composio_config):
        result = _push(ComposioSink(), _envelope({"a": 1}), composio_config)
        assert result.success is False
        assert "Missing tool" in result.error
        mock_client.execute_tool.assert_not_called()

    def test_metadata_tool_fallback(self, mock_client, composio_config):
        mock_client.execute_tool.return_value = {}
        envelope = _envelope({"a": 1}, metadata={"composio_tool": "slack_send_message"})
        result = _push(ComposioSink(), envelope, composio_config)
        assert result.success is True
        assert mock_client.execute_tool.call_args.args == ("SLACK_SEND_MESSAGE",)

    def test_params_tool_wins_over_metadata(self, mock_client, composio_config):
        mock_client.execute_tool.return_value = {}
        envelope = _envelope({"a": 1}, metadata={"composio_tool": "SLACK_SEND_MESSAGE"})
        _push(ComposioSink(), envelope, composio_config, tool="GMAIL_SEND_EMAIL")
        assert mock_client.execute_tool.call_args.args == ("GMAIL_SEND_EMAIL",)

    def test_toolkit_filter_violation_fails(self, mock_client, composio_config):
        config = {**composio_config, "toolkit_filter": ["slack"]}
        result = _push(
            ComposioSink(), _envelope({"a": 1}), config, tool="GMAIL_SEND_EMAIL"
        )
        assert result.success is False
        assert "toolkit_filter" in result.error
        mock_client.execute_tool.assert_not_called()


class TestArgumentLayering:
    def test_dict_data_is_base_arguments(self, mock_client, composio_config):
        mock_client.execute_tool.return_value = {}
        envelope = _envelope({"channel": "#general", "text": "hi"})
        _push(ComposioSink(), envelope, composio_config, tool="SLACK_SEND_MESSAGE")
        kwargs = mock_client.execute_tool.call_args.kwargs
        assert kwargs["arguments"] == {"channel": "#general", "text": "hi"}

    def test_json_string_data(self, mock_client, composio_config):
        mock_client.execute_tool.return_value = {}
        envelope = _envelope(json.dumps({"text": "hi"}))
        result = _push(
            ComposioSink(), envelope, composio_config, tool="SLACK_SEND_MESSAGE"
        )
        assert result.success is True
        assert mock_client.execute_tool.call_args.kwargs["arguments"] == {"text": "hi"}

    def test_json_bytes_data(self, mock_client, composio_config):
        mock_client.execute_tool.return_value = {}
        envelope = _envelope(json.dumps({"text": "hi"}).encode("utf-8"))
        result = _push(
            ComposioSink(), envelope, composio_config, tool="SLACK_SEND_MESSAGE"
        )
        assert result.success is True

    def test_precedence_data_metadata_params(self, mock_client, composio_config):
        mock_client.execute_tool.return_value = {}
        envelope = _envelope(
            {"a": "data", "b": "data", "c": "data"},
            metadata={"composio_arguments": {"b": "meta", "c": "meta"}},
        )
        _push(
            ComposioSink(),
            envelope,
            composio_config,
            tool="T_X",
            arguments={"c": "params"},
        )
        assert mock_client.execute_tool.call_args.kwargs["arguments"] == {
            "a": "data",
            "b": "meta",
            "c": "params",
        }

    def test_data_key_nests_text(self, mock_client, composio_config):
        mock_client.execute_tool.return_value = {}
        envelope = _envelope("hello world", content_type="text/plain")
        _push(
            ComposioSink(),
            envelope,
            composio_config,
            tool="SLACK_SEND_MESSAGE",
            data_key="text",
            arguments={"channel": "#general"},
        )
        assert mock_client.execute_tool.call_args.kwargs["arguments"] == {
            "text": "hello world",
            "channel": "#general",
        }

    def test_text_without_data_key_fails(self, mock_client, composio_config):
        envelope = _envelope("hello world", content_type="text/plain")
        result = _push(ComposioSink(), envelope, composio_config, tool="T_X")
        assert result.success is False
        assert "data_key" in result.error
        mock_client.execute_tool.assert_not_called()


class TestFileUpload:
    def test_file_arg_uploads_and_cleans_up(
        self, mock_client, composio_config, tmp_path
    ):
        mock_client.upload_dir = tmp_path / "uploads"
        mock_client.execute_tool.return_value = {"id": "file_99"}
        envelope = _envelope(
            b"\x89PNG fake",
            content_type="image/png",
            metadata={"filename": "logo.png"},
        )
        result = _push(
            ComposioSink(),
            envelope,
            composio_config,
            tool="GOOGLEDRIVE_UPLOAD_FILE",
            file_arg="file_to_upload",
        )

        assert result.success is True
        arguments = mock_client.execute_tool.call_args.kwargs["arguments"]
        upload_path = arguments["file_to_upload"]
        assert upload_path.startswith(str(tmp_path / "uploads"))
        assert upload_path.endswith("-logo.png")
        # Temp upload file is removed after execution.
        assert not (tmp_path / "uploads").exists() or not list(
            (tmp_path / "uploads").iterdir()
        )

    def test_stream_envelope_uploads(self, mock_client, composio_config, tmp_path):
        mock_client.upload_dir = tmp_path / "uploads"
        mock_client.execute_tool.return_value = {}
        envelope = _envelope(
            b"",
            content_type="application/octet-stream",
            stream=io.BytesIO(b"streamed-bytes"),
        )
        result = _push(
            ComposioSink(), envelope, composio_config, tool="T_X", file_arg="file"
        )
        assert result.success is True

    def test_stream_without_file_arg_fails(self, mock_client, composio_config):
        envelope = _envelope(b"", stream=io.BytesIO(b"x"))
        result = _push(ComposioSink(), envelope, composio_config, tool="T_X")
        assert result.success is False
        assert "file_arg" in result.error


class TestResults:
    def test_success_ref_from_result_id(self, mock_client, composio_config):
        mock_client.execute_tool.return_value = {"id": "msg_1", "ok": True}
        result = _push(
            ComposioSink(), _envelope({"a": 1}), composio_config, tool="T_X"
        )
        assert result.success is True
        assert result.ref == "msg_1"
        assert result.metadata["tool"] == "T_X"
        assert result.metadata["data"] == {"id": "msg_1", "ok": True}

    def test_success_ref_falls_back_to_slug(self, mock_client, composio_config):
        mock_client.execute_tool.return_value = {"ok": True}
        result = _push(
            ComposioSink(), _envelope({"a": 1}), composio_config, tool="SLACK_SEND_MESSAGE"
        )
        assert result.ref == "SLACK_SEND_MESSAGE"

    def test_execution_error_becomes_failed_result(self, mock_client, composio_config):
        mock_client.execute_tool.side_effect = PipelineError("tool exploded")
        result = _push(
            ComposioSink(), _envelope({"a": 1}), composio_config, tool="T_X"
        )
        assert result.success is False
        assert "tool exploded" in result.error

    def test_garbage_envelope_returns_result_not_raise(self, mock_client):
        # Compliance-style call: text/plain envelope, empty config.
        envelope = DataEnvelope(
            content_type="text/plain", data="test", source_id="compliance-test"
        )
        result = ComposioSink().push(envelope, {})
        assert result.success is False


class TestValidateConfig:
    def test_empty_config_false(self, mock_client):
        assert ComposioSink().validate_config({}) is False

    def test_valid_config_pings(self, mock_client, composio_config):
        mock_client.validate.return_value = True
        assert ComposioSink().validate_config(composio_config) is True
