"""Unit tests for ComposioTriggersSource polling behaviour."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from aisquare.pipe.core.envelope import PullParams

from aisquare_pipe_composio.triggers import ComposioTriggersSource

from tests.helpers import make_event


def _pull_once(source, config, client_unused=None, **extra_params):
    params = {"max_polls": 1, "sleep": lambda _: None, **extra_params}
    return list(source.pull(config, PullParams(params=params)))


def _config(composio_config, tmp_cursor_path, **overrides):
    return {**composio_config, "cursor_path": tmp_cursor_path, **overrides}


class TestPolling:
    def test_yields_one_envelope_per_event(
        self, mock_triggers_client, composio_config, tmp_cursor_path
    ):
        events = [make_event("evt_1"), make_event("evt_2", ts_ms=1_700_000_001_000)]
        mock_triggers_client.list_trigger_events.return_value = (events, None)
        config = _config(composio_config, tmp_cursor_path)

        envelopes = _pull_once(ComposioTriggersSource(), config)

        assert len(envelopes) == 2
        env = envelopes[0]
        assert env.content_type == "application/json"
        assert env.data["payload"] == {"subject": "hi"}
        assert env.source_id == "composio-triggers-source"
        assert env.metadata["composio_event_id"] == "evt_1"
        assert env.metadata["composio_trigger_slug"] == "GMAIL_NEW_GMAIL_MESSAGE"
        assert env.metadata["composio_toolkit"] == "gmail"
        assert env.metadata["idempotency_key"] == "composio:event:evt_1"

    def test_first_run_defaults_watermark_to_now(
        self, mock_triggers_client, composio_config, tmp_cursor_path
    ):
        mock_triggers_client.list_trigger_events.return_value = ([], None)
        _pull_once(ComposioTriggersSource(), _config(composio_config, tmp_cursor_path))
        from_ms = mock_triggers_client.list_trigger_events.call_args.kwargs["from_ms"]
        assert isinstance(from_ms, int) and from_ms > 1_700_000_000_000

    def test_since_param_sets_initial_watermark(
        self, mock_triggers_client, composio_config, tmp_cursor_path
    ):
        mock_triggers_client.list_trigger_events.return_value = ([], None)
        _pull_once(
            ComposioTriggersSource(),
            _config(composio_config, tmp_cursor_path),
            since="2023-11-14T22:13:20Z",
        )
        from_ms = mock_triggers_client.list_trigger_events.call_args.kwargs["from_ms"]
        assert from_ms == 1_700_000_000_000

    def test_watermark_persists_and_advances(
        self, mock_triggers_client, composio_config, tmp_cursor_path
    ):
        config = _config(composio_config, tmp_cursor_path)
        source = ComposioTriggersSource()

        mock_triggers_client.list_trigger_events.return_value = (
            [make_event("evt_1", ts_ms=1_700_000_005_000)],
            None,
        )
        _pull_once(source, config, since=1_700_000_000_000)

        with open(tmp_cursor_path, encoding="utf-8") as fd:
            saved = json.load(fd)
        assert saved["last_ts_ms"] == 1_700_000_005_000
        assert saved["seen_ids"] == ["evt_1"]

        mock_triggers_client.list_trigger_events.return_value = ([], None)
        _pull_once(source, config)
        from_ms = mock_triggers_client.list_trigger_events.call_args.kwargs["from_ms"]
        assert from_ms == 1_700_000_005_000

    def test_seen_events_dedupe_across_polls(
        self, mock_triggers_client, composio_config, tmp_cursor_path
    ):
        config = _config(composio_config, tmp_cursor_path)
        source = ComposioTriggersSource()
        event = make_event("evt_1", ts_ms=1_700_000_005_000)

        mock_triggers_client.list_trigger_events.return_value = ([event], None)
        assert len(_pull_once(source, config)) == 1
        # Same event re-served inside the overlap window → suppressed.
        assert len(_pull_once(source, config)) == 0

    def test_default_cursor_path_with_legacy_migration(
        self, mock_triggers_client, composio_config, tmp_path, monkeypatch
    ):
        from aisquare_pipe_composio.client import TriggerCursor, save_trigger_cursor

        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
        legacy = tmp_path / "legacy.json"
        save_trigger_cursor(
            str(legacy), TriggerCursor(last_ts_ms=1_700_000_000_000, seen_ids=["evt_0"])
        )
        monkeypatch.setattr(
            "aisquare_pipe_composio.triggers.LEGACY_CURSOR_PATH", str(legacy)
        )
        mock_triggers_client.list_trigger_events.return_value = ([], None)

        # No cursor_path in config → per-user default + one-time migration.
        envelopes = _pull_once(ComposioTriggersSource(), composio_config)

        assert envelopes == []
        kwargs = mock_triggers_client.list_trigger_events.call_args.kwargs
        assert kwargs["from_ms"] == 1_700_000_000_000  # migrated watermark used
        assert (tmp_path / "xdg" / "aisquare-pipe" / "composio-cursor.json").exists()

    def test_pagination_follows_next_cursor(
        self, mock_triggers_client, composio_config, tmp_cursor_path
    ):
        mock_triggers_client.list_trigger_events.side_effect = [
            ([make_event("evt_1")], "cur_2"),
            ([make_event("evt_2", ts_ms=1_700_000_001_000)], None),
        ]
        envelopes = _pull_once(
            ComposioTriggersSource(), _config(composio_config, tmp_cursor_path)
        )
        assert len(envelopes) == 2
        second_kwargs = mock_triggers_client.list_trigger_events.call_args_list[1].kwargs
        assert second_kwargs["cursor"] == "cur_2"


class TestFilters:
    def test_trigger_slug_filter(
        self, mock_triggers_client, composio_config, tmp_cursor_path
    ):
        events = [
            make_event("evt_1", slug="GMAIL_NEW_GMAIL_MESSAGE"),
            make_event("evt_2", slug="SLACK_NEW_MESSAGE", ts_ms=1_700_000_001_000),
        ]
        mock_triggers_client.list_trigger_events.return_value = (events, None)
        config = _config(
            composio_config, tmp_cursor_path, trigger_slugs=["gmail_new_gmail_message"]
        )

        envelopes = _pull_once(
            ComposioTriggersSource(), config, since=1_700_000_000_000
        )

        assert [e.metadata["composio_event_id"] for e in envelopes] == ["evt_1"]
        # Filtered events still advance the watermark.
        with open(tmp_cursor_path, encoding="utf-8") as fd:
            assert json.load(fd)["last_ts_ms"] == 1_700_000_001_000

    def test_toolkit_filter(
        self, mock_triggers_client, composio_config, tmp_cursor_path
    ):
        events = [
            make_event("evt_1", toolkit="gmail"),
            make_event("evt_2", toolkit="slack", ts_ms=1_700_000_001_000),
        ]
        mock_triggers_client.list_trigger_events.return_value = (events, None)
        config = _config(composio_config, tmp_cursor_path, toolkit_filter=["slack"])
        envelopes = _pull_once(ComposioTriggersSource(), config)
        assert [e.metadata["composio_event_id"] for e in envelopes] == ["evt_2"]

    def test_trigger_id_filter(
        self, mock_triggers_client, composio_config, tmp_cursor_path
    ):
        events = [make_event("evt_1"), make_event("evt_2", ts_ms=1_700_000_001_000)]
        mock_triggers_client.list_trigger_events.return_value = (events, None)
        config = _config(composio_config, tmp_cursor_path, trigger_ids=["ti_evt_2"])
        envelopes = _pull_once(ComposioTriggersSource(), config)
        assert [e.metadata["composio_event_id"] for e in envelopes] == ["evt_2"]


class TestPageCap:
    BASE = 1_700_000_000_000

    def test_page_cap_holds_watermark_and_saves_pending_cursor(
        self, mock_triggers_client, composio_config, tmp_cursor_path
    ):
        from aisquare_pipe_composio.constants import MAX_TRIGGER_PAGES_PER_POLL

        mock_triggers_client.list_trigger_events.side_effect = [
            ([make_event(f"evt_{i}", ts_ms=self.BASE + (i + 1) * 1000)], f"cur_{i}")
            for i in range(MAX_TRIGGER_PAGES_PER_POLL)
        ]
        config = _config(composio_config, tmp_cursor_path)

        envelopes = _pull_once(ComposioTriggersSource(), config, since=self.BASE)

        # Every read event is still yielded...
        assert len(envelopes) == MAX_TRIGGER_PAGES_PER_POLL
        with open(tmp_cursor_path, encoding="utf-8") as fd:
            saved = json.load(fd)
        # ...but the watermark is held back and the page cursor saved.
        assert saved["last_ts_ms"] == self.BASE
        assert saved["pending_cursor"] == f"cur_{MAX_TRIGGER_PAGES_PER_POLL - 1}"
        assert saved["pending_max_ts"] == self.BASE + MAX_TRIGGER_PAGES_PER_POLL * 1000

    def test_resume_completes_window_and_advances_watermark(
        self, mock_triggers_client, composio_config, tmp_cursor_path
    ):
        from aisquare_pipe_composio.constants import MAX_TRIGGER_PAGES_PER_POLL

        config = _config(composio_config, tmp_cursor_path)
        source = ComposioTriggersSource()

        # Poll 1: cap out mid-window.
        mock_triggers_client.list_trigger_events.side_effect = [
            ([make_event(f"evt_{i}", ts_ms=self.BASE + (i + 1) * 1000)], f"cur_{i}")
            for i in range(MAX_TRIGGER_PAGES_PER_POLL)
        ]
        _pull_once(source, config, since=self.BASE)

        # Poll 2: resumes from the saved page cursor, window drains.
        mock_triggers_client.list_trigger_events.side_effect = [
            ([make_event("evt_final", ts_ms=self.BASE + 500)], None)
        ]
        envelopes = _pull_once(source, config)

        assert [e.metadata["composio_event_id"] for e in envelopes] == ["evt_final"]
        first_resume_kwargs = mock_triggers_client.list_trigger_events.call_args.kwargs
        assert first_resume_kwargs["cursor"] == f"cur_{MAX_TRIGGER_PAGES_PER_POLL - 1}"
        assert first_resume_kwargs["from_ms"] == self.BASE

        with open(tmp_cursor_path, encoding="utf-8") as fd:
            saved = json.load(fd)
        # Watermark now advances to the max ts seen across BOTH polls.
        assert saved["pending_cursor"] is None
        assert saved["pending_max_ts"] == 0
        assert saved["last_ts_ms"] == self.BASE + MAX_TRIGGER_PAGES_PER_POLL * 1000

    def test_stale_pending_cursor_cleared_on_poll_error(
        self, mock_triggers_client, composio_config, tmp_cursor_path
    ):
        from aisquare_pipe_composio.client import TriggerCursor, save_trigger_cursor

        save_trigger_cursor(
            tmp_cursor_path,
            TriggerCursor(
                last_ts_ms=self.BASE, pending_cursor="cur_stale", pending_max_ts=123
            ),
        )
        mock_triggers_client.list_trigger_events.side_effect = Exception("expired cursor")

        envelopes = _pull_once(ComposioTriggersSource(), _config(composio_config, tmp_cursor_path))

        assert envelopes == []
        with open(tmp_cursor_path, encoding="utf-8") as fd:
            saved = json.load(fd)
        assert saved["pending_cursor"] is None
        assert saved["pending_max_ts"] == 0
        assert saved["last_ts_ms"] == self.BASE  # watermark untouched


class TestLoopBehaviour:
    def test_poll_error_logged_and_loop_continues(
        self, mock_triggers_client, composio_config, tmp_cursor_path
    ):
        mock_triggers_client.list_trigger_events.side_effect = [
            Exception("transient API blip"),
            ([make_event("evt_1")], None),
        ]
        sleeps: list[int] = []
        envelopes = list(
            ComposioTriggersSource().pull(
                _config(composio_config, tmp_cursor_path),
                PullParams(params={"max_polls": 2, "sleep": sleeps.append}),
            )
        )
        assert len(envelopes) == 1
        assert len(sleeps) == 1  # slept between polls, not after the final one

    def test_no_sleep_after_final_poll(
        self, mock_triggers_client, composio_config, tmp_cursor_path
    ):
        mock_triggers_client.list_trigger_events.return_value = ([], None)
        sleeps: list[int] = []
        list(
            ComposioTriggersSource().pull(
                _config(composio_config, tmp_cursor_path),
                PullParams(params={"max_polls": 1, "sleep": sleeps.append}),
            )
        )
        assert sleeps == []

    def test_poll_interval_config_used(
        self, mock_triggers_client, composio_config, tmp_cursor_path
    ):
        mock_triggers_client.list_trigger_events.return_value = ([], None)
        sleeps: list[int] = []
        list(
            ComposioTriggersSource().pull(
                _config(composio_config, tmp_cursor_path, poll_interval_seconds=42),
                PullParams(params={"max_polls": 2, "sleep": sleeps.append}),
            )
        )
        assert sleeps == [42]


class TestValidateAndResources:
    def test_validate_config_empty_false(self, mock_triggers_client):
        assert ComposioTriggersSource().validate_config({}) is False

    def test_list_resources_types_and_instances(
        self, mock_triggers_client, composio_config
    ):
        mock_triggers_client.list_trigger_types.return_value = [
            {
                "slug": "GMAIL_NEW_GMAIL_MESSAGE",
                "name": "New Gmail Message",
                "toolkit": {"slug": "gmail"},
                "description": "Fires on new email",
            }
        ]
        mock_triggers_client.list_trigger_instances.return_value = [
            {
                "id": "ti_1",
                "trigger_name": "GMAIL_NEW_GMAIL_MESSAGE",
                "connected_account_id": "ca_123",
                "disabled_at": None,
            }
        ]

        resources = ComposioTriggersSource().list_resources(composio_config)

        types = [r for r in resources if r.resource_type == "trigger_type"]
        instances = [r for r in resources if r.resource_type == "trigger_instance"]
        assert types[0].id == "GMAIL_NEW_GMAIL_MESSAGE"
        assert types[0].metadata["toolkit"] == "gmail"
        assert instances[0].id == "ti_1"
        assert instances[0].metadata["disabled"] is False

    def test_validate_config_pings(self, mock_triggers_client, composio_config):
        # _validate_config lives in connector.py, so patch there too.
        from unittest.mock import patch

        with patch(
            "aisquare_pipe_composio.connector.ComposioClient"
        ) as connector_client:
            instance = MagicMock()
            instance.validate.return_value = True
            connector_client.return_value = instance
            assert ComposioTriggersSource().validate_config(composio_config) is True
