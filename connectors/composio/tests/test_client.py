"""Unit tests for ComposioClient: auth, error mapping, retry, normalisation."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
from composio_client import AuthenticationError, RateLimitError

from aisquare.pipe.errors import ConfigValidationError, PipelineError

from aisquare_pipe_composio.client import (
    ComposioClient,
    TriggerCursor,
    _to_plain,
    load_trigger_cursor,
    save_trigger_cursor,
)
from aisquare_pipe_composio.constants import MAX_RETRIES, SEEN_IDS_MAX

from tests.helpers import make_list_response, make_log_response, make_raw_log_item


def _status_error(cls, status_code: int):
    """Build a Stainless status error without a live HTTP call."""
    request = httpx.Request("GET", "https://backend.composio.dev/test")
    response = httpx.Response(status_code, request=request)
    return cls("boom", response=response, body=None)


class TestInit:
    def test_missing_api_key_raises(self):
        with pytest.raises(ConfigValidationError):
            ComposioClient({})

    def test_non_string_api_key_raises(self):
        with pytest.raises(ConfigValidationError):
            ComposioClient({"api_key": 123})

    def test_sdk_kwargs(self, mock_sdk, composio_config):
        ComposioClient({**composio_config, "base_url": "https://eu.composio.dev"})
        kwargs = mock_sdk.call_args.kwargs
        assert kwargs["api_key"] == "test-key"
        assert kwargs["base_url"] == "https://eu.composio.dev"
        assert kwargs["allow_tracking"] is False
        assert "dangerously_allow_auto_upload_download_files" not in kwargs

    def test_file_mode_kwargs(self, mock_sdk, composio_config, tmp_path):
        client = ComposioClient(
            {**composio_config, "file_workdir": str(tmp_path)}, file_mode=True
        )
        kwargs = mock_sdk.call_args.kwargs
        assert kwargs["dangerously_allow_auto_upload_download_files"] is True
        assert kwargs["file_download_dir"] == str(tmp_path / "downloads")
        assert kwargs["file_upload_dirs"] == [str(tmp_path / "uploads")]
        assert client.upload_dir is not None and client.upload_dir.is_dir()
        assert client.download_dir == tmp_path / "downloads"

    def test_no_file_mode_has_no_dirs(self, mock_sdk, composio_config):
        client = ComposioClient(composio_config)
        assert client.download_dir is None
        assert client.upload_dir is None


class TestExecuteTool:
    def test_happy_path(self, mock_sdk, composio_config):
        sdk = mock_sdk.return_value
        sdk.tools.execute.return_value = {
            "data": {"messages": [1]},
            "error": None,
            "successful": True,
        }
        client = ComposioClient(composio_config)
        result = client.execute_tool(
            "GMAIL_FETCH_EMAILS", user_id="u1", arguments={"max_results": 5}
        )
        assert result == {"messages": [1]}
        args, kwargs = sdk.tools.execute.call_args
        assert args == ("GMAIL_FETCH_EMAILS", {"max_results": 5})
        assert kwargs == {"user_id": "u1"}

    def test_optional_kwargs_forwarded(self, mock_sdk, composio_config):
        sdk = mock_sdk.return_value
        sdk.tools.execute.return_value = {"data": {}, "error": None, "successful": True}
        client = ComposioClient(composio_config)
        client.execute_tool(
            "SLACK_SEND_MESSAGE",
            user_id="u1",
            arguments={},
            connected_account_id="ca_9",
            tool_version="1.2",
        )
        kwargs = sdk.tools.execute.call_args.kwargs
        assert kwargs["connected_account_id"] == "ca_9"
        assert kwargs["version"] == "1.2"

    def test_unsuccessful_raises_with_error_text(self, mock_sdk, composio_config):
        sdk = mock_sdk.return_value
        sdk.tools.execute.return_value = {
            "data": {},
            "error": "no connected account",
            "successful": False,
        }
        client = ComposioClient(composio_config)
        with pytest.raises(PipelineError, match="no connected account"):
            client.execute_tool("GMAIL_FETCH_EMAILS", user_id="u1")

    def test_auth_error_maps_to_config_validation(self, mock_sdk, composio_config):
        sdk = mock_sdk.return_value
        sdk.tools.execute.side_effect = _status_error(AuthenticationError, 401)
        client = ComposioClient(composio_config)
        with pytest.raises(ConfigValidationError, match="auth failed"):
            client.execute_tool("GMAIL_FETCH_EMAILS", user_id="u1")


class TestRetry:
    def test_retries_on_rate_limit_then_succeeds(self, mock_sdk, composio_config):
        sdk = mock_sdk.return_value
        sdk.toolkits.list.side_effect = [
            _status_error(RateLimitError, 429),
            _status_error(RateLimitError, 429),
            make_list_response([{"slug": "gmail"}]),
        ]
        client = ComposioClient(composio_config)
        with patch("aisquare_pipe_composio.client.time.sleep") as sleep:
            result = client.list_toolkits()
        assert result == [{"slug": "gmail"}]
        assert sleep.call_count == 2

    def test_gives_up_after_max_retries(self, mock_sdk, composio_config):
        sdk = mock_sdk.return_value
        sdk.toolkits.list.side_effect = _status_error(RateLimitError, 429)
        client = ComposioClient(composio_config)
        with patch("aisquare_pipe_composio.client.time.sleep") as sleep:
            with pytest.raises(PipelineError, match="rate limit"):
                client.list_toolkits()
        assert sleep.call_count == MAX_RETRIES - 1


class TestNormalisation:
    def test_to_plain_model_dump(self):
        model = MagicMock()
        model.model_dump.return_value = {"slug": "gmail"}
        assert _to_plain(model) == {"slug": "gmail"}

    def test_to_plain_recurses(self):
        model = MagicMock()
        model.model_dump.return_value = {"slug": "gmail"}
        assert _to_plain({"items": [model]}) == {"items": [{"slug": "gmail"}]}

    def test_validate_pings_toolkits(self, mock_sdk, composio_config):
        client = ComposioClient(composio_config)
        assert client.validate() is True
        mock_sdk.return_value.toolkits.list.assert_called_once_with(limit=1)


class TestTriggerEvents:
    def test_normalizes_and_sorts_events(self, mock_sdk, composio_config):
        sdk = mock_sdk.return_value
        sdk.client.logs.triggers.list.return_value = make_log_response(
            [
                make_raw_log_item("evt_2", created_at="2023-11-14T22:14:00Z"),
                make_raw_log_item("evt_1", created_at="2023-11-14T22:13:00Z"),
            ],
            next_cursor="cur_2",
        )
        client = ComposioClient(composio_config)
        events, next_cursor = client.list_trigger_events(user_id="test-user")

        assert next_cursor == "cur_2"
        assert [e["id"] for e in events] == ["evt_1", "evt_2"]
        first = events[0]
        assert first["trigger_slug"] == "GMAIL_NEW_GMAIL_MESSAGE"
        assert first["toolkit"] == "gmail"
        assert first["payload"] == {"subject": "hi"}
        assert isinstance(first["timestamp_ms"], int)

        kwargs = sdk.client.logs.triggers.list.call_args.kwargs
        assert kwargs["entity_id"] == "test-user"
        assert kwargs["include_payload"] is True

    def test_from_and_cursor_forwarded(self, mock_sdk, composio_config):
        sdk = mock_sdk.return_value
        sdk.client.logs.triggers.list.return_value = make_log_response([])
        client = ComposioClient(composio_config)
        client.list_trigger_events(from_ms=1700000000000, cursor="cur_1", limit=50)
        kwargs = sdk.client.logs.triggers.list.call_args.kwargs
        assert kwargs["from_"] == 1700000000000
        assert kwargs["cursor"] == "cur_1"
        assert kwargs["limit"] == 50

    def test_non_json_payload_kept_raw(self, mock_sdk, composio_config):
        item = make_raw_log_item("evt_1")
        item["meta"]["trigger_provider_payload"] = "not-json"
        sdk = mock_sdk.return_value
        sdk.client.logs.triggers.list.return_value = make_log_response([item])
        client = ComposioClient(composio_config)
        events, _ = client.list_trigger_events()
        assert events[0]["payload"] == "not-json"


class TestTriggerCursor:
    def test_missing_file_returns_empty(self, tmp_cursor_path):
        state = load_trigger_cursor(tmp_cursor_path)
        assert state.last_ts_ms == 0
        assert state.seen_ids == []

    def test_round_trip(self, tmp_cursor_path):
        save_trigger_cursor(
            tmp_cursor_path, TriggerCursor(last_ts_ms=123, seen_ids=["a", "b"])
        )
        state = load_trigger_cursor(tmp_cursor_path)
        assert state.last_ts_ms == 123
        assert state.seen_ids == ["a", "b"]

    def test_corrupt_file_returns_empty(self, tmp_cursor_path):
        with open(tmp_cursor_path, "w", encoding="utf-8") as fd:
            fd.write("{nope")
        state = load_trigger_cursor(tmp_cursor_path)
        assert state.last_ts_ms == 0

    def test_seen_ids_trimmed_on_save(self, tmp_cursor_path):
        ids = [f"evt_{i}" for i in range(SEEN_IDS_MAX + 100)]
        save_trigger_cursor(tmp_cursor_path, TriggerCursor(last_ts_ms=1, seen_ids=ids))
        with open(tmp_cursor_path, encoding="utf-8") as fd:
            payload = json.load(fd)
        assert len(payload["seen_ids"]) == SEEN_IDS_MAX
        assert payload["seen_ids"][-1] == ids[-1]
