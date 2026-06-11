"""Unit tests for the toolkit-pinned connector factories."""

from __future__ import annotations

import pytest

from aisquare.pipe.core.envelope import DataEnvelope, PullParams, PushParams

from aisquare_pipe_composio.connector import ComposioSink, ComposioSource
from aisquare_pipe_composio.factory import composio_sink, composio_source


class TestFactoryShape:
    def test_source_class_attributes(self):
        cls = composio_source("gmail")
        assert cls.__name__ == "ComposioGmailSource"
        assert issubclass(cls, ComposioSource)
        assert cls.name == "composio-gmail-source"
        assert cls.toolkit == "gmail"
        assert "gmail" in cls.description
        assert cls.docs_url.endswith("/toolkits/gmail")
        assert cls.version == ComposioSource.version
        assert cls.auth_type == ComposioSource.auth_type

    def test_sink_class_attributes(self):
        cls = composio_sink("slack")
        assert cls.__name__ == "ComposioSlackSink"
        assert issubclass(cls, ComposioSink)
        assert cls.name == "composio-slack-sink"
        assert cls.toolkit == "slack"

    def test_classes_are_independent(self):
        gmail = composio_source("gmail")
        slack = composio_source("slack")
        assert gmail is not slack
        assert gmail.toolkit == "gmail"
        assert slack.toolkit == "slack"
        assert ComposioSource.toolkit is None

    def test_zero_arg_instantiable(self):
        instance = composio_source("gmail")()
        assert instance.name == "composio-gmail-source"

    def test_input_normalized(self):
        assert composio_source("  Gmail ").toolkit == "gmail"

    def test_multiword_slug_class_name(self):
        cls = composio_source("google_maps")
        assert cls.__name__ == "ComposioGoogleMapsSource"
        assert cls.name == "composio-google_maps-source"

    @pytest.mark.parametrize("bad", ["", "  ", "no spaces allowed", "ünïcode", "a/b"])
    def test_invalid_toolkit_arg_raises(self, bad):
        with pytest.raises(ValueError, match="toolkit slug"):
            composio_source(bad)


class TestPinEnforcement:
    def test_pinned_source_accepts_own_toolkit(self, mock_client, composio_config):
        mock_client.execute_tool.return_value = {}
        source = composio_source("gmail")()
        envelopes = list(
            source.pull(
                composio_config, PullParams(params={"tool": "GMAIL_FETCH_EMAILS"})
            )
        )
        assert len(envelopes) == 1
        assert envelopes[0].source_id == "composio-gmail-source"
        assert envelopes[0].metadata["composio_toolkit"] == "gmail"

    def test_pinned_source_rejects_other_toolkit(self, mock_client, composio_config):
        source = composio_source("gmail")()
        gen = source.pull(
            composio_config, PullParams(params={"tool": "SLACK_SEND_MESSAGE"})
        )
        with pytest.raises(ValueError, match="pinned to 'gmail'"):
            next(gen)
        mock_client.execute_tool.assert_not_called()

    def test_prefix_match_is_exact(self, mock_client, composio_config):
        """A 'google' pin must not match GOOGLECALENDAR_* tools."""
        source = composio_source("google")()
        gen = source.pull(
            composio_config,
            PullParams(params={"tool": "GOOGLECALENDAR_LIST_EVENTS"}),
        )
        with pytest.raises(ValueError, match="pinned to 'google'"):
            next(gen)

    def test_hyphenated_pin_matches_underscore_prefix(
        self, mock_client, composio_config
    ):
        mock_client.execute_tool.return_value = {}
        source = composio_source("google-maps")()
        envelopes = list(
            source.pull(
                composio_config,
                PullParams(params={"tool": "GOOGLE_MAPS_SEARCH"}),
            )
        )
        assert len(envelopes) == 1

    def test_pinned_sink_rejects_other_toolkit(self, mock_client, composio_config):
        sink = composio_sink("slack")()
        envelope = DataEnvelope(
            content_type="application/json", data={"a": 1}, source_id="t"
        )
        result = sink.push(
            envelope, composio_config, PushParams(params={"tool": "GMAIL_SEND_EMAIL"})
        )
        assert result.success is False
        assert "pinned to 'slack'" in result.error
        mock_client.execute_tool.assert_not_called()

    def test_pinned_sink_accepts_own_toolkit(self, mock_client, composio_config):
        mock_client.execute_tool.return_value = {}
        sink = composio_sink("slack")()
        envelope = DataEnvelope(
            content_type="application/json", data={"text": "hi"}, source_id="t"
        )
        result = sink.push(
            envelope,
            composio_config,
            PushParams(params={"tool": "SLACK_SEND_MESSAGE"}),
        )
        assert result.success is True
        assert result.metadata["toolkit"] == "slack"

    def test_pinned_list_resources_scopes_to_toolkit(
        self, mock_client, composio_config
    ):
        from tests.helpers import make_tool, make_toolkit

        mock_client.list_toolkits.return_value = [
            make_toolkit("gmail"),
            make_toolkit("slack"),
        ]
        mock_client.list_connected_accounts.return_value = []
        mock_client.list_tools.return_value = [make_tool("GMAIL_FETCH_EMAILS")]

        resources = composio_source("gmail")().list_resources(composio_config)

        toolkit_ids = [r.id for r in resources if r.resource_type == "toolkit"]
        assert toolkit_ids == ["gmail"]
        mock_client.list_tools.assert_called_once_with(toolkit="gmail")
