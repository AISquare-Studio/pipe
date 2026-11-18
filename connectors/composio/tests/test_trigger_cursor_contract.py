"""The trigger cursor's host contract.

pipe-as-service's poller keeps the triggers source's position in its own
store: each poll it writes the stored position with ``save_trigger_cursor``
to a fresh ``cursor_path``, runs one ``pull(max_polls=1)``, and reads back
what the pull wrote with ``load_trigger_cursor``. That round trip is a
cross-repo contract, so these names are public (``__all__``) and pinned here:
a field added to ``TriggerCursor``, a field ``save``/``load`` drops, or a
changed first-run rule fails this file, not silently in the host.
"""

from __future__ import annotations

import json
from dataclasses import fields

from aisquare.pipe.core.envelope import PullParams

import aisquare_pipe_composio
from aisquare_pipe_composio import client as client_module
from aisquare_pipe_composio import constants
from aisquare_pipe_composio.client import (
    TriggerCursor,
    load_trigger_cursor,
    save_trigger_cursor,
)
from aisquare_pipe_composio.triggers import ComposioTriggersSource
from tests.helpers import make_event

# Epoch ms (``since`` reads a small number as seconds).
START = 1_763_158_380_000

HOST_CONTRACT = {
    "MAX_EVENT_CLOCK_SKEW_MS",
    "MAX_TRIGGER_PAGES_PER_POLL",
    "SEEN_IDS_MAX",
    "TriggerCursor",
    "is_transient_error",
    "load_trigger_cursor",
    "save_trigger_cursor",
}


def test_the_host_contract_is_exported():
    """Importable from the package itself, as the defining modules' objects."""
    assert set(aisquare_pipe_composio.TRIGGER_CURSOR_API) == HOST_CONTRACT
    assert HOST_CONTRACT <= set(aisquare_pipe_composio.__all__)
    for name in HOST_CONTRACT:
        defining = constants if name.isupper() else client_module
        assert getattr(aisquare_pipe_composio, name) is getattr(defining, name)


def test_the_cursor_fields_are_pinned():
    """The host copies each of these both ways. A new one must be added to
    the host first (pipe-as-service pins this list too)."""
    assert [(f.name, f.type) for f in fields(TriggerCursor)] == [
        ("last_ts_ms", "int"),
        ("seen_ids", "list[str]"),
        ("pending_cursor", "str | None"),
        ("pending_max_ts", "int"),
    ]


def test_save_then_load_keeps_every_field(tmp_path):
    """No field is dropped on the way through the file. Built from
    ``fields()``, so a field added without save/load support fails here."""
    samples = {
        "int": 1_763_158_380_000,
        "list[str]": ["e1", "e2"],
        "str | None": "cur_7",
    }
    written = TriggerCursor(
        **{f.name: samples[str(f.type)] for f in fields(TriggerCursor)}
    )
    path = str(tmp_path / "cursor.json")

    save_trigger_cursor(path, written)

    assert load_trigger_cursor(path) == written
    assert set(json.loads((tmp_path / "cursor.json").read_text())) == {
        f.name for f in fields(TriggerCursor)
    }


def test_save_keeps_the_newest_ids(tmp_path):
    path = str(tmp_path / "cursor.json")
    ring = [f"e{n}" for n in range(constants.SEEN_IDS_MAX + 7)]

    save_trigger_cursor(path, TriggerCursor(last_ts_ms=1, seen_ids=ring))

    assert load_trigger_cursor(path).seen_ids == ring[-constants.SEEN_IDS_MAX :]


def test_the_ring_holds_two_whole_pulls():
    """One pull reads up to MAX_TRIGGER_PAGES_PER_POLL pages of the default
    100. The ring holds two, so a window read again is recognised (it was
    500: half of one pull)."""
    one_pull = (
        constants.MAX_TRIGGER_PAGES_PER_POLL * constants.DEFAULT_TRIGGER_PAGE_LIMIT
    )
    assert constants.SEEN_IDS_MAX >= 2 * one_pull


def _pull_once(config, **params):
    pull = PullParams(params={"max_polls": 1, "sleep": lambda _s: None, **params})
    return list(ComposioTriggersSource().pull(config, pull))


def test_a_first_run_starts_at_since(mock_triggers_client, composio_config, tmp_path):
    """No file, or one with last_ts_ms 0 and no ids: the pull starts at
    ``since`` (the host's start: a subscription's creation, or now)."""
    mock_triggers_client.list_trigger_events.return_value = ([], None)
    path = str(tmp_path / "cursor.json")
    config = {**composio_config, "cursor_path": path}

    _pull_once(config, since=START)
    save_trigger_cursor(path, TriggerCursor())
    _pull_once(config, since=START + 1)

    froms = [
        c.kwargs["from_ms"]
        for c in mock_triggers_client.list_trigger_events.call_args_list
    ]
    assert froms == [START, START + 1]


def test_a_host_written_position_is_resumed_as_is(
    mock_triggers_client, composio_config, tmp_path
):
    """The stored watermark is the ``from``, the page cursor the ``cursor``,
    and the ids are skipped. ``since`` is ignored once there is a position."""
    path = str(tmp_path / "cursor.json")
    save_trigger_cursor(
        path,
        TriggerCursor(
            last_ts_ms=START + 5_000,
            seen_ids=["e1"],
            pending_cursor="cur_3",
            pending_max_ts=START + 7_000,
        ),
    )
    mock_triggers_client.list_trigger_events.return_value = (
        [make_event("e1", ts_ms=START + 6_000), make_event("e2", ts_ms=START + 8_000)],
        None,
    )

    envelopes = _pull_once({**composio_config, "cursor_path": path}, since=START)

    kwargs = mock_triggers_client.list_trigger_events.call_args.kwargs
    assert (kwargs["from_ms"], kwargs["cursor"]) == (START + 5_000, "cur_3")
    assert [e.metadata["composio_event_id"] for e in envelopes] == ["e2"]
    assert load_trigger_cursor(path) == TriggerCursor(
        last_ts_ms=START + 8_000, seen_ids=["e1", "e2"]
    )
