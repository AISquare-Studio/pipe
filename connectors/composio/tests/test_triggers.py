"""Unit tests for ComposioTriggersSource polling behaviour."""

from __future__ import annotations

import json
import logging
import os
import time
from unittest.mock import MagicMock

import pytest

from aisquare.pipe.core.envelope import PullParams
from aisquare.pipe.errors import ConfigValidationError, PipelineError

from aisquare_pipe_composio.client import ComposioSDKShapeError
from aisquare_pipe_composio.triggers import ComposioTriggersSource

from tests.helpers import make_event


def _pull_once(source, config, client_unused=None, **extra_params):
    params = {"max_polls": 1, "sleep": lambda _: None, **extra_params}
    return list(source.pull(config, PullParams(params=params)))


def _config(composio_config, tmp_cursor_path, **overrides):
    return {**composio_config, "cursor_path": tmp_cursor_path, **overrides}


def _api_error(status: int) -> PipelineError:
    """What ComposioClient raises when Composio answers ``status``: a
    PipelineError chained to the SDK's own status error."""
    import httpx
    from composio_client import APIStatusError

    request = httpx.Request("POST", "https://composio.invalid/api/v3.1/internal/trigger/logs")
    cause = APIStatusError(
        f"HTTP {status}", response=httpx.Response(status, request=request), body=None
    )
    error = PipelineError(f"Composio API error: HTTP {status}")
    error.__cause__ = cause
    return error


def _saved(path):
    with open(path, encoding="utf-8") as fd:
        return json.load(fd)


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


    def test_a_transient_failure_resumes_on_the_page_it_stopped_on(
        self, mock_triggers_client, composio_config, tmp_cursor_path
    ):
        """A 503 on the second page of a resumed window: the cursor keeps
        that page and the ids read before it, and the next pull reads on from
        there. Before: the page cursor was dropped, and the next pull
        rescanned the whole window from the held watermark (with 1500 entries
        and the old 500-id ring, 500 of them were delivered twice)."""
        from aisquare_pipe_composio.client import TriggerCursor, save_trigger_cursor

        save_trigger_cursor(
            tmp_cursor_path,
            TriggerCursor(
                last_ts_ms=self.BASE, seen_ids=["evt_0"], pending_cursor="cur_9",
                pending_max_ts=self.BASE + 900,
            ),
        )
        config = _config(composio_config, tmp_cursor_path)
        source = ComposioTriggersSource()
        mock_triggers_client.list_trigger_events.side_effect = [
            ([make_event("evt_a", ts_ms=self.BASE + 1000)], "cur_10"),
            _api_error(503),
        ]

        first = _pull_once(source, config)

        assert [e.metadata["composio_event_id"] for e in first] == ["evt_a"]
        assert _saved(tmp_cursor_path) == {
            "last_ts_ms": self.BASE,  # held: the window isn't drained
            "seen_ids": ["evt_0", "evt_a"],
            "pending_cursor": "cur_10",
            "pending_max_ts": self.BASE + 1000,
        }

        mock_triggers_client.list_trigger_events.side_effect = [
            ([make_event("evt_b", ts_ms=self.BASE + 2000)], None)
        ]
        second = _pull_once(source, config)
        kwargs = mock_triggers_client.list_trigger_events.call_args.kwargs
        assert (kwargs["cursor"], kwargs["from_ms"]) == ("cur_10", self.BASE)
        assert [e.metadata["composio_event_id"] for e in second] == ["evt_b"]
        assert _saved(tmp_cursor_path)["last_ts_ms"] == self.BASE + 2000

    def test_a_transient_failure_mid_window_saves_the_ids_read(
        self, mock_triggers_client, composio_config, tmp_cursor_path
    ):
        """No window was pending: page 1 is read, page 2 times out. Before,
        nothing was saved, so a host that keeps the position itself saw no
        trace of page 1's ids."""
        mock_triggers_client.list_trigger_events.side_effect = [
            ([make_event("evt_1", ts_ms=self.BASE + 1000)], "cur_2"),
            _api_error(500),
        ]

        _pull_once(ComposioTriggersSource(), _config(composio_config, tmp_cursor_path), since=self.BASE)

        assert _saved(tmp_cursor_path) == {
            "last_ts_ms": self.BASE,
            "seen_ids": ["evt_1"],
            "pending_cursor": "cur_2",
            "pending_max_ts": self.BASE + 1000,
        }

    @pytest.mark.parametrize("status", [400, 404, 422])
    def test_a_request_composio_refuses_drops_the_page_cursor(
        self, mock_triggers_client, composio_config, tmp_cursor_path, status
    ):
        """A 4xx on the resumed page (an expired cursor) would fail every
        cycle: the cursor is dropped and the window rescanned, as before."""
        from aisquare_pipe_composio.client import TriggerCursor, save_trigger_cursor

        save_trigger_cursor(
            tmp_cursor_path,
            TriggerCursor(last_ts_ms=self.BASE, pending_cursor="cur_stale", pending_max_ts=9),
        )
        mock_triggers_client.list_trigger_events.side_effect = _api_error(status)

        _pull_once(ComposioTriggersSource(), _config(composio_config, tmp_cursor_path))

        saved = _saved(tmp_cursor_path)
        assert (saved["pending_cursor"], saved["pending_max_ts"]) == (None, 0)
        assert saved["last_ts_ms"] == self.BASE

    def test_on_cycle_error_hears_of_a_failed_cycle_after_its_save(
        self, mock_triggers_client, composio_config, tmp_cursor_path
    ):
        """A one-cycle pull doesn't raise a failed read; ``on_cycle_error``
        tells the host, once the cycle's progress is on disk."""
        error = _api_error(502)
        mock_triggers_client.list_trigger_events.side_effect = [
            ([make_event("evt_1", ts_ms=self.BASE + 1000)], "cur_2"),
            error,
        ]
        heard: list[tuple[Exception, dict]] = []

        envelopes = _pull_once(
            ComposioTriggersSource(),
            _config(composio_config, tmp_cursor_path),
            since=self.BASE,
            on_cycle_error=lambda e: heard.append((e, _saved(tmp_cursor_path))),
        )

        assert len(envelopes) == 1
        [(got, on_disk)] = heard
        assert got is error
        assert on_disk["pending_cursor"] == "cur_2" and on_disk["seen_ids"] == ["evt_1"]

    def test_on_cycle_error_is_not_called_for_a_good_cycle(
        self, mock_triggers_client, composio_config, tmp_cursor_path
    ):
        mock_triggers_client.list_trigger_events.return_value = ([], None)
        heard: list[Exception] = []
        _pull_once(
            ComposioTriggersSource(),
            _config(composio_config, tmp_cursor_path),
            on_cycle_error=heard.append,
        )
        assert heard == []


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


class TestNonRetryableErrors:
    @pytest.mark.parametrize(
        "error",
        [
            ComposioSDKShapeError("no trigger-log resource"),
            ConfigValidationError("Composio auth failed: 401 provider says bad key"),
        ],
        ids=["sdk-shape", "auth"],
    )
    def test_stop_polling_with_one_error_line_and_no_traceback(
        self, mock_triggers_client, composio_config, tmp_cursor_path, caplog, error
    ):
        """Raised on cycle 1. One ERROR line says why, without the message
        (it may carry Composio's response text) or a traceback: both travel
        with the raised error, which the caller logs once."""
        mock_triggers_client.list_trigger_events.side_effect = error
        params = PullParams(params={"max_polls": 5, "sleep": lambda _: None})
        with (
            caplog.at_level(logging.DEBUG, logger="aisquare.pipe.composio"),
            pytest.raises(type(error)),
        ):
            config = _config(composio_config, tmp_cursor_path)
            list(ComposioTriggersSource().pull(config, params))

        assert mock_triggers_client.list_trigger_events.call_count == 1
        (logged,) = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert logged.exc_info is None
        assert "trigger pull stopped" in logged.getMessage()
        assert str(error) not in logged.getMessage()


class TestMalformedEvents:
    """list_trigger_events returns a malformed entry as an event with
    ``skipped`` set (and only its id and time)."""

    BASE = 1_700_000_000_000

    @staticmethod
    def _skipped(event_id, ts_ms):
        return {
            "id": event_id,
            "timestamp": None,
            "timestamp_ms": ts_ms,
            "skipped": "malformed trigger-log entry (its meta is a str, not an object)",
        }

    def test_skipped_event_is_recorded_and_paged_past(
        self, mock_triggers_client, composio_config, tmp_cursor_path, caplog
    ):
        mock_triggers_client.list_trigger_events.side_effect = [
            ([self._skipped("evt_bad", self.BASE + 9000)], "cur_2"),
            ([make_event("evt_ok", ts_ms=self.BASE + 1000)], None),
        ]
        with caplog.at_level(logging.WARNING, logger="aisquare.pipe.composio"):
            envelopes = _pull_once(
                ComposioTriggersSource(),
                _config(composio_config, tmp_cursor_path),
                since=self.BASE,
            )

        assert [e.metadata["composio_event_id"] for e in envelopes] == ["evt_ok"]
        second = mock_triggers_client.list_trigger_events.call_args_list[1].kwargs
        assert second["cursor"] == "cur_2"
        with open(tmp_cursor_path, encoding="utf-8") as fd:
            saved = json.load(fd)
        assert saved["seen_ids"] == ["evt_bad", "evt_ok"]
        assert saved["last_ts_ms"] == self.BASE + 9000
        assert ["evt_bad" in r.getMessage() for r in caplog.records] == [True]

    def test_skipped_event_without_an_id_is_warned_about_not_recorded(
        self, mock_triggers_client, composio_config, tmp_cursor_path, caplog
    ):
        mock_triggers_client.list_trigger_events.side_effect = [
            ([self._skipped(None, None)], "cur_2"),
            ([make_event("evt_ok", ts_ms=self.BASE + 1000)], None),
        ]
        with caplog.at_level(logging.WARNING, logger="aisquare.pipe.composio"):
            envelopes = _pull_once(
                ComposioTriggersSource(),
                _config(composio_config, tmp_cursor_path),
                since=self.BASE,
            )

        assert [e.metadata["composio_event_id"] for e in envelopes] == ["evt_ok"]
        with open(tmp_cursor_path, encoding="utf-8") as fd:
            assert json.load(fd)["seen_ids"] == ["evt_ok"]
        (warned,) = caplog.records
        assert warned.levelno == logging.WARNING and "(no id)" in warned.getMessage()

    @pytest.mark.parametrize(
        "no_id",
        [
            {"id": None, "timestamp_ms": BASE + 9000, "skipped": "malformed (meta)"},
            # A readable entry with no id: never yielded, and now warned about.
            make_event("", ts_ms=BASE + 9000),
        ],
        ids=["skipped", "readable"],
    )
    def test_an_entry_without_an_id_still_moves_the_watermark(
        self, mock_triggers_client, composio_config, tmp_cursor_path, caplog, no_id
    ):
        """It can't be recorded, so each cycle that reads it warns; its time
        still moves the window past it. Before: the watermark stayed at the
        last event with an id (BASE + 1000), and a readable one was dropped
        without a word."""
        mock_triggers_client.list_trigger_events.return_value = (
            [make_event("evt_ok", ts_ms=self.BASE + 1000), no_id],
            None,
        )
        with caplog.at_level(logging.WARNING, logger="aisquare.pipe.composio"):
            envelopes = _pull_once(
                ComposioTriggersSource(),
                _config(composio_config, tmp_cursor_path),
                since=self.BASE,
            )

        assert [e.metadata["composio_event_id"] for e in envelopes] == ["evt_ok"]
        with open(tmp_cursor_path, encoding="utf-8") as fd:
            saved = json.load(fd)
        assert saved["seen_ids"] == ["evt_ok"]
        assert saved["last_ts_ms"] == self.BASE + 9000
        (warned,) = caplog.records
        assert "(no id)" in warned.getMessage()


class TestFutureTimestamps:
    """No real event is dated past now + MAX_EVENT_CLOCK_SKEW_MS. Before, one
    such time (malformed data, or a bad clock) became the watermark, and every
    later poll sent that `from` and saw nothing: a silently blind poller."""

    FAR_FUTURE = 253_370_764_800_000  # 9999-01-01

    def test_a_future_event_time_does_not_move_the_watermark(
        self, mock_triggers_client, composio_config, tmp_cursor_path
    ):
        now_ms = int(time.time() * 1000)
        mock_triggers_client.list_trigger_events.return_value = (
            [
                make_event("evt_1", ts_ms=now_ms - 60_000),
                make_event("evt_future", ts_ms=self.FAR_FUTURE),
                make_event("evt_bad", ts_ms=self.FAR_FUTURE, skipped="malformed"),
            ],
            None,
        )
        config = _config(composio_config, tmp_cursor_path)
        source = ComposioTriggersSource()

        envelopes = _pull_once(source, config, since=now_ms - 120_000)
        assert [e.metadata["composio_event_id"] for e in envelopes] == [
            "evt_1",
            "evt_future",
        ]
        with open(tmp_cursor_path, encoding="utf-8") as fd:
            assert json.load(fd)["last_ts_ms"] == now_ms - 60_000

        mock_triggers_client.list_trigger_events.return_value = ([], None)
        _pull_once(source, config)
        from_ms = mock_triggers_client.list_trigger_events.call_args.kwargs["from_ms"]
        assert from_ms == now_ms - 60_000

    def test_a_time_within_the_clock_skew_moves_it(
        self, mock_triggers_client, composio_config, tmp_cursor_path
    ):
        """A guard (passes before too): a real event a minute ahead of this
        host's clock (clock skew) still moves the watermark."""
        ahead = int(time.time() * 1000) + 60_000
        mock_triggers_client.list_trigger_events.return_value = (
            [make_event("evt_1", ts_ms=ahead)],
            None,
        )
        _pull_once(
            ComposioTriggersSource(),
            _config(composio_config, tmp_cursor_path),
            since=ahead - 1000,
        )
        with open(tmp_cursor_path, encoding="utf-8") as fd:
            assert json.load(fd)["last_ts_ms"] == ahead

    def test_a_stored_future_watermark_is_reset_to_now(
        self, mock_triggers_client, composio_config, tmp_cursor_path, caplog
    ):
        with open(tmp_cursor_path, "w", encoding="utf-8") as fd:
            json.dump(
                {
                    "last_ts_ms": self.FAR_FUTURE,
                    "seen_ids": ["evt_0"],
                    "pending_cursor": None,
                    "pending_max_ts": self.FAR_FUTURE,
                },
                fd,
            )
        mock_triggers_client.list_trigger_events.return_value = ([], None)
        before = int(time.time() * 1000)

        with caplog.at_level(logging.WARNING, logger="aisquare.pipe.composio"):
            _pull_once(
                ComposioTriggersSource(), _config(composio_config, tmp_cursor_path)
            )

        from_ms = mock_triggers_client.list_trigger_events.call_args.kwargs["from_ms"]
        assert before <= from_ms <= int(time.time() * 1000)
        with open(tmp_cursor_path, encoding="utf-8") as fd:
            saved = json.load(fd)
        assert saved["last_ts_ms"] == from_ms and saved["pending_max_ts"] == 0
        assert "reset to now" in caplog.text


class TestSeenRing:
    def test_an_entry_read_on_every_poll_stays_in_the_ring(
        self, mock_triggers_client, composio_config, tmp_cursor_path, monkeypatch
    ):
        """An entry dated past the ceiling never moves the watermark, so every
        poll reads it again. Each read moves its id to the ring's newest end.
        Before: it was skipped without that, newer reads pushed it out of the
        ring, and it was delivered again (here on poll 3 with a 4-id ring)."""
        from aisquare_pipe_composio import client as client_module

        monkeypatch.setattr(client_module, "SEEN_IDS_MAX", 4)
        now_ms = int(time.time() * 1000)
        future = make_event("evt_future", ts_ms=TestFutureTimestamps.FAR_FUTURE)
        config = _config(composio_config, tmp_cursor_path)
        source = ComposioTriggersSource()
        delivered: list[str] = []
        for poll in range(5):
            new = [make_event(f"evt_{poll}_{n}", ts_ms=now_ms - 60_000 + poll * 10 + n) for n in range(3)]
            mock_triggers_client.list_trigger_events.return_value = ([*new, future], None)
            envelopes = _pull_once(source, config, since=now_ms - 120_000)
            delivered += [e.metadata["composio_event_id"] for e in envelopes]

        assert delivered.count("evt_future") == 1
        assert len(delivered) == 16
        assert _saved(tmp_cursor_path)["seen_ids"][-1] == "evt_future"

    def test_reading_the_newest_id_again_is_no_change(
        self, mock_triggers_client, composio_config, tmp_cursor_path
    ):
        """The inclusive ``from`` returns the last entry on every quiet poll:
        already the newest id, so the cursor isn't rewritten for it."""
        mock_triggers_client.list_trigger_events.return_value = (
            [make_event("evt_1", ts_ms=TestPageCap.BASE + 1)],
            None,
        )
        config = _config(composio_config, tmp_cursor_path)
        _pull_once(ComposioTriggersSource(), config, since=TestPageCap.BASE)
        written = os.stat(tmp_cursor_path).st_mtime_ns
        os.utime(tmp_cursor_path, ns=(0, 0))

        _pull_once(ComposioTriggersSource(), config)

        assert written and os.stat(tmp_cursor_path).st_mtime_ns == 0  # not saved again


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
