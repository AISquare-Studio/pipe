"""ComposioClient against the REAL composio SDK classes (no MagicMock SDK).

The other client tests mock ``composio.Composio`` itself, so they can't see
the SDK move a resource: composio-client 2.x moved ``client.logs.triggers``
to ``client._internal.logs.triggers``, and ``list_trigger_events`` raised
AttributeError there. Here the real ``Composio`` is built, offline: every
socket connect is refused and counted, and the base URL is ``.invalid``.
Two kinds of test:

- An SDK method is replaced by an autospec of the real one (every method
  this connector calls), so a call that doesn't bind to the installed SDK's
  signature fails.
- ``httpx.Client.send`` is faked (``composio_http``), so the SDK's own
  request, retry and response-parsing code runs on scripted answers.

Runs under whichever composio / composio-client pair is installed.
validate.yml's ``composio-sdk`` job runs it twice: on composio 0.22.0 +
composio-client 1.43.0, and on the newest pair the ``[composio]`` extra
resolves.
"""

from __future__ import annotations

import json
import logging
import socket
import time
from collections.abc import Callable
from importlib.metadata import version
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

import httpx
import pytest
from aisquare_pipe_composio.client import (
    ComposioClient,
    ComposioSDKShapeError,
    _epoch_ms,
)
from aisquare_pipe_composio.constants import MAX_RETRIES, TRIGGER_LOG_SDK_RETRIES

from aisquare.pipe.core.envelope import PullParams
from aisquare.pipe.errors import ConfigValidationError, PipelineError

CLIENT_MAJOR = int(version("composio-client").split(".")[0])
# Unresolvable by design (RFC 6761): even a leak couldn't reach Composio.
OFFLINE_CONFIG = {"api_key": "ck_test", "base_url": "https://composio.invalid"}


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


class FakeComposioHTTP:
    """Answers the SDK's HTTP requests from ``respond`` and records them.
    ``respond(request, body)`` returns ``(status, json_body)``, or
    ``(status, bytes)`` for a raw JSON body."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self.respond: Callable[[httpx.Request, dict], tuple[int, Any]] = (
            lambda request, body: (200, {"data": [], "nextCursor": None})
        )

    def send(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content or b"{}")
        status, answer = self.respond(request, body)
        if isinstance(answer, bytes):
            headers = {"content-type": "application/json"}
            return httpx.Response(
                status, content=answer, headers=headers, request=request
            )
        return httpx.Response(status, json=answer, request=request)


@pytest.fixture
def composio_http(monkeypatch, sockets):
    """The SDK's transport, faked: no request leaves the process, and retry
    backoff doesn't sleep. Every request must be a trigger-log read."""
    fake = FakeComposioHTTP()
    monkeypatch.setattr(
        httpx.Client, "send", lambda _client, request, **_kw: fake.send(request)
    )
    monkeypatch.setattr(time, "sleep", fake.sleeps.append)
    yield fake
    assert sockets == []
    assert all(r.url.host == "composio.invalid" for r in fake.requests)
    assert all(r.url.path.endswith("/internal/trigger/logs") for r in fake.requests)


def _pages(pages: dict[str | None, tuple[list, str | None]]):
    """A ``respond`` serving trigger-log pages by the request's cursor."""

    def respond(_request: httpx.Request, body: dict) -> tuple[int, Any]:
        items, next_cursor = pages[body.get("cursor")]
        return 200, {"data": items, "nextCursor": next_cursor}

    return respond


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

    return TriggerListResponse.model_validate(
        {"data": items, "nextCursor": next_cursor}
    )


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

    # 2.x's list() takes request_options; 1.x's doesn't (it would not bind).
    retries = (
        {"request_options": {"max_retries": TRIGGER_LOG_SDK_RETRIES}}
        if CLIENT_MAJOR >= 2
        else {}
    )
    listed.assert_called_once_with(
        limit=50,
        include_payload=True,
        status="success",
        entity_id="test-user",
        from_=1_700_000_000_000,
        cursor="cur_1",
        **retries,
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


def test_an_sdk_without_a_trigger_log_resource_is_a_pipeline_error(
    mock_sdk, composio_config
):
    """An SDK shape this connector doesn't know: a PipelineError naming what
    it looked for, never a bare AttributeError."""
    mock_sdk.return_value.client = SimpleNamespace(logs=SimpleNamespace())
    client = ComposioClient(composio_config)
    with pytest.raises(ComposioSDKShapeError) as raised:
        client.list_trigger_events(user_id="test-user")
    assert isinstance(raised.value, PipelineError)
    assert "client._internal.logs.triggers" in str(raised.value)


def test_an_unexpected_sdk_signature_is_a_pipeline_error(mock_sdk, composio_config):
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
    from aisquare_pipe_composio.triggers import ComposioTriggersSource

    mock_triggers_client.list_trigger_events.side_effect = ComposioSDKShapeError(
        "no logs"
    )
    config = {"api_key": "ck_test", "cursor_path": str(tmp_path / "cursor.json")}
    params = PullParams(params={"max_polls": 5, "sleep": lambda _s: None})
    with (
        caplog.at_level(logging.ERROR, logger="aisquare.pipe.composio"),
        pytest.raises(ComposioSDKShapeError),
    ):
        list(ComposioTriggersSource().pull(config, params))
    assert mock_triggers_client.list_trigger_events.call_count == 1
    assert any(r.levelno == logging.ERROR for r in caplog.records)


# -- a malformed trigger-log entry is skipped, never an SDK error (m1) ---------
# (The SDK's own model_dump warns about the malformed fields: expected.)


def _bad_meta_item(event_id: str, created_at: str) -> dict:
    item = _log_item(event_id, created_at)
    item["meta"] = "not an object"
    return item


@pytest.mark.filterwarnings("ignore:Pydantic serializer warnings:UserWarning")
@pytest.mark.parametrize(
    "bad, skipped_id, reason",
    [
        (
            _bad_meta_item("evt_bad", "2026-11-14T22:14:00Z"),
            "evt_bad",
            "its meta is a str",
        ),
        ("not an object", None, "the entry is a str"),
    ],
    ids=["meta-not-an-object", "entry-not-an-object"],
)
def test_a_malformed_trigger_log_entry_is_skipped_not_an_sdk_error(
    composio_http, bad, skipped_id, reason
):
    """Before: ComposioSDKShapeError ("unsupported composio SDK") on both
    lines, from our own parsing of API data."""
    good = _log_item("evt_ok", "2026-11-14T22:13:00Z")
    composio_http.respond = _pages({None: ([bad, good], "cur_2")})

    events, next_cursor = ComposioClient(OFFLINE_CONFIG).list_trigger_events(
        user_id="test-user"
    )

    assert next_cursor == "cur_2"
    assert [e["id"] for e in events if not e.get("skipped")] == ["evt_ok"]
    (skipped,) = [e for e in events if e.get("skipped")]
    assert skipped["id"] == skipped_id
    assert reason in skipped["skipped"]


@pytest.mark.filterwarnings("ignore:Pydantic serializer warnings:UserWarning")
def test_an_unreadable_timestamp_is_no_time_not_an_error(composio_http):
    """``createdAt: 1e400`` is valid JSON that parses to inf. Before: int(inf)
    raised OverflowError out of list_trigger_events on both lines."""
    item = _log_item("evt_1", "PLACEHOLDER")
    raw = json.dumps({"data": [item], "nextCursor": None}).replace(
        '"PLACEHOLDER"', "1e400"
    )
    composio_http.respond = lambda _request, _body: (200, raw.encode())

    client = ComposioClient(OFFLINE_CONFIG)
    (event,), _ = client.list_trigger_events(user_id="test-user")

    assert event["id"] == "evt_1" and event["timestamp_ms"] is None
    assert not event.get("skipped")


@pytest.mark.filterwarnings("ignore:Pydantic serializer warnings:UserWarning")
def test_the_poller_steps_past_a_malformed_event(composio_http, tmp_path, caplog):
    """The page whose only entry is malformed still leads to the next page,
    the bad entry's id and time advance the cursor, and it is warned about
    once across two polls. Before: the poller stopped on cycle 1 with
    "unsupported composio SDK", and a restart re-read the same entry."""
    from aisquare_pipe_composio.triggers import ComposioTriggersSource

    composio_http.respond = _pages(
        {
            None: ([_bad_meta_item("evt_bad", "2026-11-14T22:14:00Z")], "cur_2"),
            "cur_2": ([_log_item("evt_ok", "2026-11-14T22:13:00Z")], None),
        }
    )
    cursor = tmp_path / "cursor.json"
    config = {**OFFLINE_CONFIG, "cursor_path": str(cursor)}
    source = ComposioTriggersSource()

    def poll(**params: Any) -> list:
        pull = PullParams(params={"max_polls": 1, "sleep": lambda _s: None, **params})
        return list(source.pull(config, pull))

    with caplog.at_level(logging.WARNING, logger="aisquare.pipe.composio"):
        first = poll(since=0)
        second = poll()

    assert [e.metadata["composio_event_id"] for e in first] == ["evt_ok"]
    assert second == []
    saved = json.loads(cursor.read_text())
    assert saved["seen_ids"] == ["evt_bad", "evt_ok"]
    assert saved["last_ts_ms"] == _epoch_ms("2026-11-14T22:14:00Z")
    warned = [r for r in caplog.records if "evt_bad" in r.getMessage()]
    assert [r.levelno for r in warned] == [logging.WARNING]
    assert "not an object" in warned[0].getMessage()


# -- the trigger-log read keeps 1.x's SDK retries on 2.x (m3) ------------------


@pytest.mark.parametrize(
    "status, attempts",
    [
        (503, 1 + TRIGGER_LOG_SDK_RETRIES),
        (429, (1 + TRIGGER_LOG_SDK_RETRIES) * MAX_RETRIES),
    ],
    ids=["503", "429"],
)
def test_the_trigger_log_read_keeps_its_sdk_retries(composio_http, status, attempts):
    """2.x gives a POST no SDK retries; this read asks for 1.x's. A 429 is
    also retried by our own wrapper (MAX_RETRIES). Before, on 2.x: 1 and 5
    attempts. 1.x already made 3 and 15."""
    composio_http.respond = lambda _request, _body: (
        status,
        {"error": {"message": "busy"}},
    )

    with pytest.raises(PipelineError):
        ComposioClient(OFFLINE_CONFIG).list_trigger_events(user_id="test-user")

    assert len(composio_http.requests) == attempts


# -- Composio refusing the key stops the poller (m4) ---------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_the_poller_stops_when_composio_refuses_the_key(
    composio_http, tmp_path, status
):
    """Before: 4 polls, 4 "trigger poll cycle failed", nothing raised."""
    from aisquare_pipe_composio.triggers import ComposioTriggersSource

    composio_http.respond = lambda _request, _body: (
        status,
        {"error": {"message": "bad key"}},
    )
    config = {**OFFLINE_CONFIG, "cursor_path": str(tmp_path / "cursor.json")}
    params = PullParams(params={"max_polls": 4, "sleep": lambda _s: None})

    with pytest.raises(ConfigValidationError):
        list(ComposioTriggersSource().pull(config, params))

    assert len(composio_http.requests) == 1


# -- only SDK access is a ComposioSDKShapeError --------------------------------


def test_a_type_error_inside_the_sdk_call_is_not_an_sdk_shape_error(
    sockets, monkeypatch
):
    """The arguments bind (checked first), so a TypeError is the SDK's own,
    not drift. Before: any TypeError from list() was a shape error."""
    client = ComposioClient(OFFLINE_CONFIG)
    resource = _installed_trigger_logs(client)
    failing = create_autospec(
        resource.list, side_effect=TypeError("raised inside the SDK")
    )
    monkeypatch.setattr(resource, "list", failing)

    with pytest.raises(TypeError, match="raised inside the SDK"):
        client.list_trigger_events(user_id="test-user")
    assert failing.call_count == 1 and sockets == []


def test_an_attribute_error_inside_an_sdk_call_is_not_an_sdk_shape_error(
    sockets, monkeypatch
):
    """Before: every AttributeError out of a client method was a shape error."""
    client = ComposioClient(OFFLINE_CONFIG)
    toolkits = client._sdk.toolkits
    failing = create_autospec(
        toolkits.list, side_effect=AttributeError("raised inside the SDK")
    )
    monkeypatch.setattr(toolkits, "list", failing)

    with pytest.raises(AttributeError, match="raised inside the SDK"):
        client.list_toolkits()
    assert sockets == []


# -- every other SDK call binds to the installed SDK ---------------------------

_CONNECTION_REQUEST = SimpleNamespace(
    id="cr_1", status="INITIATED", redirect_url="https://example.invalid/auth"
)
_EMPTY_PAGE = SimpleNamespace(items=[], next_cursor=None)

# (what we call, the SDK object it reaches, its method, what that returns,
# the exact call expected).
SDK_CALLS = [
    pytest.param(
        lambda c: c.validate(),
        lambda sdk: sdk.toolkits,
        "list",
        _EMPTY_PAGE,
        ((), {"limit": 1}),
        id="validate",
    ),
    pytest.param(
        lambda c: c.list_toolkits(limit=10),
        lambda sdk: sdk.toolkits,
        "list",
        _EMPTY_PAGE,
        ((), {"limit": 10}),
        id="list_toolkits",
    ),
    pytest.param(
        lambda c: c.execute_tool(
            "GMAIL_SEND_EMAIL",
            user_id="u",
            arguments={"to": "a@example.invalid"},
            connected_account_id="ca_1",
            tool_version="20251101_00",
        ),
        lambda sdk: sdk.tools,
        "execute",
        {"successful": True, "data": {"id": "m_1"}},
        (
            ("GMAIL_SEND_EMAIL", {"to": "a@example.invalid"}),
            {"user_id": "u", "connected_account_id": "ca_1", "version": "20251101_00"},
        ),
        id="execute_tool-pinned",
    ),
    pytest.param(
        lambda c: c.execute_tool("GMAIL_SEND_EMAIL", user_id="u"),
        lambda sdk: sdk.tools,
        "execute",
        {"successful": True, "data": {}},
        (
            ("GMAIL_SEND_EMAIL", {}),
            {"user_id": "u", "dangerously_skip_version_check": True},
        ),
        id="execute_tool-latest",
    ),
    pytest.param(
        lambda c: c.list_tools(toolkit="gmail", search="send", limit=5),
        lambda sdk: sdk.tools,
        "get_raw_composio_tools",
        [],
        ((), {"toolkits": ["gmail"], "search": "send", "limit": 5}),
        id="list_tools",
    ),
    pytest.param(
        lambda c: c.get_tool("GMAIL_SEND_EMAIL"),
        lambda sdk: sdk.tools,
        "get_raw_composio_tool_by_slug",
        {"slug": "GMAIL_SEND_EMAIL"},
        (("GMAIL_SEND_EMAIL",), {}),
        id="get_tool",
    ),
    pytest.param(
        lambda c: c.list_connected_accounts(user_id="u", toolkits=["gmail"]),
        lambda sdk: sdk.connected_accounts,
        "list",
        _EMPTY_PAGE,
        ((), {"user_ids": ["u"], "toolkit_slugs": ["gmail"]}),
        id="list_connected_accounts",
    ),
    pytest.param(
        lambda c: c.authorize_toolkit(user_id="u", toolkit="gmail"),
        lambda sdk: sdk.toolkits,
        "authorize",
        _CONNECTION_REQUEST,
        ((), {"user_id": "u", "toolkit": "gmail"}),
        id="authorize_toolkit",
    ),
    pytest.param(
        lambda c: c.initiate_connection(
            user_id="u",
            auth_config_id="ac_1",
            callback_url="https://example.invalid/cb",
        ),
        lambda sdk: sdk.connected_accounts,
        "initiate",
        _CONNECTION_REQUEST,
        (("u", "ac_1"), {"callback_url": "https://example.invalid/cb"}),
        id="initiate_connection",
    ),
    pytest.param(
        lambda c: c.wait_for_connection("ca_1", timeout=5.0),
        lambda sdk: sdk.connected_accounts,
        "wait_for_connection",
        {"id": "ca_1"},
        (("ca_1",), {"timeout": 5.0}),
        id="wait_for_connection",
    ),
    pytest.param(
        lambda c: c.list_auth_configs(toolkit="gmail"),
        lambda sdk: sdk.auth_configs,
        "list",
        _EMPTY_PAGE,
        ((), {"toolkit_slug": "gmail"}),
        id="list_auth_configs",
    ),
    pytest.param(
        lambda c: c.list_trigger_types(toolkits=["gmail"]),
        lambda sdk: sdk.triggers,
        "list",
        _EMPTY_PAGE,
        ((), {"toolkit_slugs": ["gmail"]}),
        id="list_trigger_types",
    ),
    pytest.param(
        lambda c: c.list_trigger_instances(trigger_ids=["ti_1"]),
        lambda sdk: sdk.triggers,
        "list_active",
        _EMPTY_PAGE,
        ((), {"trigger_ids": ["ti_1"]}),
        id="list_trigger_instances",
    ),
]


@pytest.mark.parametrize("call, owner, method, returns, expected", SDK_CALLS)
def test_every_sdk_call_binds_to_the_installed_sdk(
    sockets, monkeypatch, call, owner, method, returns, expected
):
    """Each wrapper's SDK call against an autospec of the installed SDK's
    real method: an argument it doesn't take fails here (and in the code,
    which binds before calling) instead of in production."""
    client = ComposioClient(OFFLINE_CONFIG)
    resource = owner(client._sdk)
    spec = create_autospec(getattr(resource, method), return_value=returns)
    monkeypatch.setattr(resource, method, spec)

    call(client)

    args, kwargs = expected
    spec.assert_called_once_with(*args, **kwargs)
    assert sockets == []


def test_auth_configs_list_takes_toolkit_slug():
    """auth_configs.list takes ``**query: Unpack[AuthConfigListParams]``, which
    binds any keyword, so check the key the TypedDict declares."""
    from composio_client.types.auth_config_list_params import AuthConfigListParams

    assert "toolkit_slug" in AuthConfigListParams.__annotations__
