"""Unit tests for ComposioSource.pull / list_resources / validate_config."""

from __future__ import annotations

import pytest

from aisquare.pipe.core.envelope import PullParams

from aisquare_pipe_composio.connector import ComposioSource

from tests.helpers import make_account, make_tool, make_toolkit


def _pull(source, config, **params):
    return list(source.pull(config, PullParams(params=params)))


class TestPullBasics:
    def test_missing_tool_raises_lazily(self, mock_client, composio_config):
        source = ComposioSource()
        gen = source.pull(composio_config, PullParams())
        with pytest.raises(ValueError, match="requires params\\['tool'\\]"):
            next(gen)
        mock_client.execute_tool.assert_not_called()

    def test_slug_is_normalized(self, mock_client, composio_config):
        mock_client.execute_tool.return_value = {"ok": True}
        source = ComposioSource()
        _pull(source, composio_config, tool="  gmail_fetch_emails ")
        assert mock_client.execute_tool.call_args.args == ("GMAIL_FETCH_EMAILS",)

    def test_single_envelope_by_default(self, mock_client, composio_config):
        mock_client.execute_tool.return_value = {"messages": [{"id": 1}, {"id": 2}]}
        source = ComposioSource()
        envelopes = _pull(source, composio_config, tool="GMAIL_FETCH_EMAILS")

        assert len(envelopes) == 1
        env = envelopes[0]
        assert env.content_type == "application/json"
        assert env.data == {"messages": [{"id": 1}, {"id": 2}]}
        assert env.source_id == "composio-source"
        assert env.metadata["composio_tool"] == "GMAIL_FETCH_EMAILS"
        assert env.metadata["composio_toolkit"] == "gmail"
        assert env.metadata["composio_user_id"] == "test-user"

    def test_non_dict_result_is_wrapped(self, mock_client, composio_config):
        mock_client.execute_tool.return_value = [1, 2, 3]
        source = ComposioSource()
        envelopes = _pull(source, composio_config, tool="HACKERNEWS_GET_FRONTPAGE")
        assert envelopes[0].data == {"value": [1, 2, 3]}

    def test_arguments_and_overrides_forwarded(self, mock_client, composio_config):
        mock_client.execute_tool.return_value = {}
        source = ComposioSource()
        _pull(
            source,
            composio_config,
            tool="GMAIL_FETCH_EMAILS",
            arguments={"max_results": 5},
            user_id="other-user",
            connected_account_id="ca_42",
            tool_version="2.0",
        )
        kwargs = mock_client.execute_tool.call_args.kwargs
        assert kwargs["arguments"] == {"max_results": 5}
        assert kwargs["user_id"] == "other-user"
        assert kwargs["connected_account_id"] == "ca_42"
        assert kwargs["tool_version"] == "2.0"

    def test_config_user_id_default(self, mock_client, composio_config):
        mock_client.execute_tool.return_value = {}
        source = ComposioSource()
        _pull(source, composio_config, tool="GMAIL_FETCH_EMAILS")
        assert mock_client.execute_tool.call_args.kwargs["user_id"] == "test-user"


class TestUnwrap:
    def test_unwrap_true_single_list_key(self, mock_client, composio_config):
        mock_client.execute_tool.return_value = {"messages": [{"id": 1}, {"id": 2}]}
        source = ComposioSource()
        envelopes = _pull(
            source, composio_config, tool="GMAIL_FETCH_EMAILS", unwrap=True
        )
        assert len(envelopes) == 2
        assert envelopes[0].data == {"id": 1}
        assert envelopes[0].metadata["item_index"] == 0
        assert envelopes[0].metadata["item_count"] == 2
        assert envelopes[1].metadata["item_index"] == 1

    def test_unwrap_true_bare_list(self, mock_client, composio_config):
        mock_client.execute_tool.return_value = ["a", "b"]
        source = ComposioSource()
        envelopes = _pull(source, composio_config, tool="T_X", unwrap=True)
        assert len(envelopes) == 2
        assert envelopes[0].data == {"value": "a"}

    def test_unwrap_true_falls_back_to_single(self, mock_client, composio_config):
        mock_client.execute_tool.return_value = {"a": 1, "b": [2]}
        source = ComposioSource()
        envelopes = _pull(source, composio_config, tool="T_X", unwrap=True)
        assert len(envelopes) == 1
        assert envelopes[0].data == {"a": 1, "b": [2]}

    def test_unwrap_path(self, mock_client, composio_config):
        mock_client.execute_tool.return_value = {
            "response": {"items": [{"id": 1}, {"id": 2}, {"id": 3}]}
        }
        source = ComposioSource()
        envelopes = _pull(
            source, composio_config, tool="T_X", unwrap="response.items"
        )
        assert len(envelopes) == 3

    def test_unwrap_path_not_a_list_raises(self, mock_client, composio_config):
        mock_client.execute_tool.return_value = {"response": {"items": "nope"}}
        source = ComposioSource()
        with pytest.raises(ValueError, match="expected a list"):
            _pull(source, composio_config, tool="T_X", unwrap="response.items")

    def test_unwrap_path_missing_key_raises(self, mock_client, composio_config):
        mock_client.execute_tool.return_value = {"response": {}}
        source = ComposioSource()
        with pytest.raises(ValueError, match="not found"):
            _pull(source, composio_config, tool="T_X", unwrap="response.items")


class TestToolkitGovernance:
    def test_toolkit_filter_rejects(self, mock_client, composio_config):
        config = {**composio_config, "toolkit_filter": ["slack", "github"]}
        source = ComposioSource()
        with pytest.raises(ValueError, match="not allowed by toolkit_filter"):
            _pull(source, config, tool="GMAIL_FETCH_EMAILS")
        mock_client.execute_tool.assert_not_called()

    def test_toolkit_filter_allows(self, mock_client, composio_config):
        config = {**composio_config, "toolkit_filter": ["gmail"]}
        mock_client.execute_tool.return_value = {}
        source = ComposioSource()
        envelopes = _pull(source, config, tool="GMAIL_FETCH_EMAILS")
        assert len(envelopes) == 1


class TestDownloadFiles:
    def test_file_envelopes_after_json(self, mock_client, composio_config, tmp_path):
        download_dir = tmp_path / "downloads"
        attachment = download_dir / "gmail" / "GMAIL_GET_ATTACHMENT" / "report.pdf"
        attachment.parent.mkdir(parents=True)
        attachment.write_bytes(b"%PDF-1.4 fake")

        mock_client.download_dir = download_dir
        mock_client.execute_tool.return_value = {"file": str(attachment)}

        source = ComposioSource()
        envelopes = _pull(
            source,
            composio_config,
            tool="GMAIL_GET_ATTACHMENT",
            download_files=True,
        )

        assert len(envelopes) == 2
        json_env, file_env = envelopes
        assert json_env.content_type == "application/json"
        assert file_env.content_type == "application/pdf"
        assert file_env.data == b"%PDF-1.4 fake"
        assert file_env.metadata["filename"] == "report.pdf"
        assert file_env.metadata["file_field"] == "file"
        assert file_env.metadata["composio_tool"] == "GMAIL_GET_ATTACHMENT"

    def test_no_file_envelopes_without_flag(self, mock_client, composio_config):
        mock_client.execute_tool.return_value = {"file": {"s3url": "https://..."}}
        source = ComposioSource()
        envelopes = _pull(source, composio_config, tool="GMAIL_GET_ATTACHMENT")
        assert len(envelopes) == 1


class TestValidateConfig:
    def test_empty_config_is_false_without_network(self, mock_client):
        source = ComposioSource()
        assert source.validate_config({}) is False
        mock_client.validate.assert_not_called()

    def test_valid_config_pings(self, mock_client, composio_config):
        mock_client.validate.return_value = True
        assert ComposioSource().validate_config(composio_config) is True

    def test_failed_ping_is_false(self, mock_client, composio_config):
        mock_client.validate.side_effect = Exception("nope")
        assert ComposioSource().validate_config(composio_config) is False


class TestListResources:
    def test_toolkits_with_connection_status(self, mock_client, composio_config):
        mock_client.list_toolkits.return_value = [
            make_toolkit("gmail"),
            make_toolkit("slack"),
        ]
        mock_client.list_connected_accounts.return_value = [make_account("gmail")]

        resources = ComposioSource().list_resources(composio_config)

        by_id = {r.id: r for r in resources}
        assert by_id["gmail"].resource_type == "toolkit"
        assert by_id["gmail"].metadata["connected"] is True
        assert by_id["gmail"].metadata["connection_status"] == "ACTIVE"
        assert by_id["gmail"].metadata["connected_account_id"] == "ca_123"
        assert by_id["slack"].metadata["connected"] is False
        assert by_id["slack"].metadata["connection_status"] == "NOT_CONNECTED"
        # No toolkit filter → no per-tool resources.
        assert all(r.resource_type == "toolkit" for r in resources)

    def test_tools_listed_with_filter(self, mock_client, composio_config):
        config = {**composio_config, "toolkit_filter": ["gmail"]}
        mock_client.list_toolkits.return_value = [
            make_toolkit("gmail"),
            make_toolkit("slack"),
        ]
        mock_client.list_connected_accounts.return_value = []
        mock_client.list_tools.return_value = [
            make_tool("GMAIL_FETCH_EMAILS"),
            make_tool("GMAIL_SEND_EMAIL"),
        ]

        resources = ComposioSource().list_resources(config)

        toolkits = [r for r in resources if r.resource_type == "toolkit"]
        tools = [r for r in resources if r.resource_type == "tool"]
        assert [r.id for r in toolkits] == ["gmail"]
        assert {r.id for r in tools} == {"GMAIL_FETCH_EMAILS", "GMAIL_SEND_EMAIL"}
        mock_client.list_tools.assert_called_once_with(toolkit="gmail")


class TestRateLimit:
    def test_rate_limit_advertised(self):
        limit = ComposioSource().rate_limit()
        assert limit is not None
        assert limit.requests_per_second == 5
