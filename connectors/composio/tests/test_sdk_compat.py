"""ComposioClient against the REAL composio SDK classes (no MagicMock SDK).

The other client tests mock ``composio.Composio`` itself, so they can't see
the SDK move a resource: composio-client 2.x moved ``client.logs.triggers``
to ``client._internal.logs.triggers``, and ``list_trigger_events`` raised
AttributeError there. Here the real ``Composio`` is built (offline: every
socket connect is refused and counted), and only the trigger-log resource's
``list`` is replaced, by an autospec of the real method, so a call that
doesn't bind to the installed SDK's signature fails. Runs under whichever
composio / composio-client pair is installed (CI: 0.22.0 + 1.43.0 and
0.33.x + 2.1.x).
"""

from __future__ import annotations

import logging
import socket
from importlib.metadata import version
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest
from aisquare_pipe_composio.client import ComposioClient

from aisquare.pipe.core.envelope import PullParams
from aisquare.pipe.errors import PipelineError

CLIENT_MAJOR = int(version("composio-client").split(".")[0])


@pytest.fixture
def sockets(monkeypatch):
    """Every socket connect is refused, and recorded: a test asserts none."""
    attempts: list[object] = []

    def refuse(*args, **kwargs):
        attempts.append(args)
        raise OSError("sockets are blocked in this test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    return attempts


def _installed_trigger_logs(client: ComposioClient):
    """The installed SDK's trigger-log resource, found from its version alone
    (not from the code under test): 2.x keeps it under ``_internal``."""
    http = client._sdk.client
    owner = http._internal.logs if CLIENT_MAJOR >= 2 else http.logs
    return owner.triggers


def _real_response(items: list[dict], next_cursor: str | None):
    """A real ``TriggerListResponse`` (the same model in 1.x and 2.x), built
    from the wire JSON the API answers with (camelCase)."""
    from composio_client.types.logs.trigger_list_response import TriggerListResponse

    return TriggerListResponse.model_validate({"data": items, "nextCursor": next_cursor})


def _log_item(event_id: str, created_at: str) -> dict:
    """One trigger-log entry as the API sends it."""
    return {
        "id": event_id,
        "appName": "gmail",
        "clientId": "cl_1",
        "connectionId": "ca_1",
        "createdAt": created_at,
        "entityId": "test-user",
        "status": "success",
        "type": "trigger",
        "meta": {
            "id": event_id,
            "clientId": "cl_1",
            "connectionId": "ca_1",
            "createdAt": created_at,
            "updatedAt": created_at,
            "provider": "gmail",
            "type": "trigger",
            "triggerId": "ti_1",
            "triggerName": "GMAIL_NEW_GMAIL_MESSAGE",
            "triggerNanoId": "ti_1",
            "triggerProviderPayload": '{"subject": "hi"}',
        },
    }


def test_list_trigger_events_binds_to_the_installed_sdk(sockets, monkeypatch):
    client = ComposioClient({"api_key": "ck_test"})  # the real composio.Composio
    resource = _installed_trigger_logs(client)
    response = _real_response(
        [
            _log_item("evt_2", "2026-11-14T22:14:00Z"),
            _log_item("evt_1", "2026-11-14T22:13:00Z"),
        ],
        next_cursor="cur_2",
    )
    listed = create_autospec(resource.list, return_value=response)
    monkeypatch.setattr(resource, "list", listed)

    events, next_cursor = client.list_trigger_events(
        user_id="test-user", from_ms=1_700_000_000_000, cursor="cur_1", limit=50
    )

    listed.assert_called_once_with(
        limit=50,
        include_payload=True,
        status="success",
        entity_id="test-user",
        from_=1_700_000_000_000,
        cursor="cur_1",
    )
    assert next_cursor == "cur_2"
    assert [e["id"] for e in events] == ["evt_1", "evt_2"]
    assert events[0]["trigger_slug"] == "GMAIL_NEW_GMAIL_MESSAGE"
    assert events[0]["payload"] == {"subject": "hi"}
    assert sockets == []


def test_the_triggers_source_polls_the_installed_sdk(sockets, monkeypatch, tmp_path):
    """The poller's source, end to end on the real SDK: events come out (on
    2.x before the fix it logged the AttributeError and emitted nothing)."""
    from aisquare_pipe_composio.triggers import ComposioTriggersSource

    built: list[ComposioClient] = []
    response = _real_response([_log_item("evt_1", "2026-11-14T22:13:00Z")], None)

    class Recording(ComposioClient):
        def __init__(self, config, **kwargs):
            super().__init__(config, **kwargs)
            resource = _installed_trigger_logs(self)
            monkeypatch.setattr(
                resource, "list", create_autospec(resource.list, return_value=response)
            )
            built.append(self)

    monkeypatch.setattr("aisquare_pipe_composio.triggers.ComposioClient", Recording)
    source = ComposioTriggersSource()
    config = {"api_key": "ck_test", "cursor_path": str(tmp_path / "cursor.json")}
    params = PullParams(params={"max_polls": 1, "sleep": lambda _s: None, "since": 0})
    envelopes = list(source.pull(config, params))
    assert [e.metadata["composio_event_id"] for e in envelopes] == ["evt_1"]
    assert len(built) == 1 and sockets == []


def test_an_sdk_without_a_trigger_log_resource_is_a_pipeline_error(mock_sdk, composio_config):
    """An SDK shape this connector doesn't know: a PipelineError naming what
    it looked for, never a bare AttributeError."""
    from aisquare_pipe_composio.client import ComposioSDKShapeError

    mock_sdk.return_value.client = SimpleNamespace(logs=SimpleNamespace())
    client = ComposioClient(composio_config)
    with pytest.raises(ComposioSDKShapeError) as raised:
        client.list_trigger_events(user_id="test-user")
    assert isinstance(raised.value, PipelineError)
    assert "client._internal.logs.triggers" in str(raised.value)


def test_an_unexpected_sdk_signature_is_a_pipeline_error(mock_sdk, composio_config):
    from aisquare_pipe_composio.client import ComposioSDKShapeError

    def list_without_our_arguments(*, time=None):  # a drifted signature
        return SimpleNamespace(data=[], next_cursor=None)

    triggers = SimpleNamespace(list=list_without_our_arguments)
    mock_sdk.return_value.client = SimpleNamespace(
        _internal=SimpleNamespace(logs=SimpleNamespace(triggers=triggers))
    )
    client = ComposioClient(composio_config)
    with pytest.raises(ComposioSDKShapeError):
        client.list_trigger_events(user_id="test-user")


def test_the_poller_surfaces_an_sdk_shape_error_instead_of_polling_forever(
    mock_triggers_client, caplog, tmp_path
):
    """Logged at ERROR and raised on the first cycle: never swallowed."""
    from aisquare_pipe_composio.client import ComposioSDKShapeError
    from aisquare_pipe_composio.triggers import ComposioTriggersSource

    mock_triggers_client.list_trigger_events.side_effect = ComposioSDKShapeError("no logs")
    config = {"api_key": "ck_test", "cursor_path": str(tmp_path / "cursor.json")}
    params = PullParams(params={"max_polls": 5, "sleep": lambda _s: None})
    with (
        caplog.at_level(logging.ERROR, logger="aisquare.pipe.composio"),
        pytest.raises(ComposioSDKShapeError),
    ):
        list(ComposioTriggersSource().pull(config, params))
    assert mock_triggers_client.list_trigger_events.call_count == 1
    assert any(r.levelno == logging.ERROR for r in caplog.records)
