"""Failure contract: every failure class carries core's canonical
``error_code`` (and ``retry_after`` when throttled); every argument refusal
is ``invalid_arguments``; the host's ``config["timeout_seconds"]`` bounds a
whole call; concurrent calls share no HTTP session."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import pytest
import requests
from aisquare.pipe import ERROR_CODES

from aisquare_pipe_onedrive import OneDriveToolkit, rest
from aisquare_pipe_onedrive.rest import OneDriveAPIError, OneDriveRestClient
from aisquare_pipe_onedrive.toolkit import error_code, time_budget

from .fakes import (
    CONFIG,
    MONITOR_URL,
    TOKEN,
    FakeSession,
    file_item,
    folder_item,
    graph_error,
    harness,
    response,
)

ITEM_ID = "01BYE5RZ6QN3ZWBTUFOFD3GSPGOHDJD36K"
GET = ("ONE_DRIVE_GET_ITEM", {"item_id": ITEM_ID})
POST = ("ONE_DRIVE_ONEDRIVE_CREATE_FOLDER", {"name": "Q4"})
DELETE = ("ONE_DRIVE_DELETE_ITEM", {"item_id": ITEM_ID})


def _throttled(status: int, retry_after: str | None, n: int = 1):
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return [graph_error(status, "activityLimitReached", "The request has been throttled", headers=headers) for _ in range(n)]


# (id, tool call, scripted answers, error_code, retry_after)
FAILURES = [
    ("401", GET, [graph_error(401, "InvalidAuthenticationToken", "Access token has expired or is not yet valid.")], "unauthorized", None),
    ("403", GET, [graph_error(403, "accessDenied", "Access denied")], "forbidden", None),
    ("404", GET, [graph_error(404, "itemNotFound", "The resource could not be found.")], "not_found", None),
    ("400", GET, [graph_error(400, "invalidRequest", "Invalid request")], "invalid_arguments", None),
    ("409 name clash", POST, [graph_error(409, "nameAlreadyExists", "Name already exists")], "conflict", None),
    ("412 stale eTag", DELETE, [graph_error(412, "preconditionFailed", "ETag does not match")], "conflict", None),
    ("423 locked", DELETE, [graph_error(423, "resourceLocked", "The resource is locked")], "conflict", None),
    ("429 Retry-After beyond the cap", GET, _throttled(429, "120"), "rate_limited", 120.0),
    ("429, retries exhausted", GET, _throttled(429, "5", 3), "rate_limited", 5.0),
    ("429 without Retry-After, retries exhausted", GET, _throttled(429, None, 3), "rate_limited", 5.0),
    ("503 with Retry-After, retries exhausted", GET, _throttled(503, "7", 3), "rate_limited", 7.0),
    ("509 bandwidth limit", POST, _throttled(509, "30", 3), "rate_limited", 30.0),
    ("503 without Retry-After x3 on a GET", GET, [graph_error(503, "serviceNotAvailable", "down")] * 3, "upstream_error", None),
    ("500", GET, [graph_error(500, "generalException", "boom")], "upstream_error", None),
    ("502 on a POST", POST, [response(502, content=b"")], "upstream_error", None),
    ("504", POST, [response(504, content=b"")], "timeout", None),
    ("507 quota full", POST, [graph_error(507, "quotaLimitReached", "Insufficient Storage")], "upstream_error", None),
    ("ReadTimeout", GET, [requests.ReadTimeout("read timed out")], "timeout", None),
    ("ConnectTimeout", GET, [requests.ConnectTimeout("connect timed out")], "timeout", None),
    ("ConnectionError", GET, [requests.ConnectionError("refused")], "upstream_error", None),
    ("200 that is not JSON", GET, [response(200, content=b"<html>", content_type="text/html")], "upstream_error", None),
    (
        "copy reported failed",
        ("ONE_DRIVE_COPY_ITEM", {"item_id": ITEM_ID}),
        [response(202, headers={"Location": MONITOR_URL}), response(200, {"status": "failed", "error": {"code": "generalException", "message": "copy broke"}})],
        "upstream_error",
        None,
    ),
    (
        "copy name clash",
        ("ONE_DRIVE_COPY_ITEM", {"item_id": ITEM_ID}),
        [response(202, headers={"Location": MONITOR_URL}), response(200, {"status": "failed", "error": {"code": "nameAlreadyExists", "message": "exists"}})],
        "conflict",
        None,
    ),
]


@pytest.mark.parametrize(
    ("call", "answers", "code", "retry_after"),
    [f[1:] for f in FAILURES],
    ids=[f[0] for f in FAILURES],
)
def test_error_code_per_failure_class(call, answers, code, retry_after):
    slug, args = call
    h = harness(*answers)
    result = h.call(slug, args)
    assert result.successful is False
    assert result.error_code == code
    assert result.error_code in ERROR_CODES
    assert result.retry_after == retry_after
    assert TOKEN not in result.error
    assert result.to_dict()["error_code"] == code


def test_name_clash_says_what_to_do():
    h = harness(graph_error(409, "nameAlreadyExists", "Name already exists"))
    result = h.call("ONE_DRIVE_UPDATE_DRIVE_ITEM_METADATA", {"item_id": ITEM_ID, "name": "taken.pdf"})
    assert "conflict_behavior" in result.error


ARGUMENT_ERRORS = [
    ("dot item_id", "ONE_DRIVE_GET_ITEM", {"item_id": ".."}, []),
    ("slash in item_id", "ONE_DRIVE_DELETE_ITEM", {"item_id": "01A/../../children"}, []),
    ("dot drive_id", "ONE_DRIVE_GET_ITEM", {"item_id": ITEM_ID, "drive_id": "."}, []),
    ("dot segment in item_path", "ONE_DRIVE_GET_ITEM_BY_PATH", {"item_path": "Documents/../../secret"}, []),
    ("dot folder_path", "ONE_DRIVE_LIST_FOLDER_CHILDREN", {"folder_path": "/Documents/.."}, []),
    ("dot upload folder", "ONE_DRIVE_ONEDRIVE_UPLOAD_FILE", {"file_name": "a.txt", "content": "x", "folder": "../x"}, []),
    ("path in file_name", "ONE_DRIVE_ONEDRIVE_UPLOAD_FILE", {"file_name": "../a.txt", "content": "x"}, []),
    ("bad base64", "ONE_DRIVE_ONEDRIVE_UPLOAD_FILE", {"file_name": "a.bin", "content": "not base64!", "encoding": "base64"}, []),
    ("wildcard-only search", "ONE_DRIVE_SEARCH_ITEMS", {"q": " * "}, []),
    ("download of a folder", "ONE_DRIVE_DOWNLOAD_FILE", {"item_id": ITEM_ID}, [response(200, folder_item())]),
    ("download too large", "ONE_DRIVE_DOWNLOAD_FILE", {"item_id": ITEM_ID}, [response(200, file_item(size=200 * 1024 * 1024))]),
    ("foreign page_token", "ONE_DRIVE_LIST_FOLDER_CHILDREN", {"page_token": "https://evil.example/steal"}, []),
    ("dot page_token", "ONE_DRIVE_LIST_FOLDER_CHILDREN", {"page_token": "/me/drive/../../me/messages"}, []),
    ("non-drive page_token", "ONE_DRIVE_SEARCH_ITEMS", {"q": "x", "page_token": "/admin/reports"}, []),
    ("move without destination id", "ONE_DRIVE_MOVE_ITEM", {"itemId": ITEM_ID, "parentReference": {"name": "Archive"}}, []),
    ("copy children_only with name", "ONE_DRIVE_COPY_ITEM", {"item_id": ITEM_ID, "children_only": True, "name": "x"}, []),
    ("update with nothing", "ONE_DRIVE_UPDATE_DRIVE_ITEM_METADATA", {"item_id": ITEM_ID}, []),
    ("update drive without parent", "ONE_DRIVE_UPDATE_DRIVE_ITEM_METADATA", {"item_id": ITEM_ID, "parent_reference_drive_id": "b!x"}, []),
    ("users link without recipients", "ONE_DRIVE_CREATE_LINK", {"item_id": ITEM_ID, "type": "view", "scope": "users"}, []),
    (
        "anonymous link with recipients",
        "ONE_DRIVE_CREATE_LINK",
        {"item_id": ITEM_ID, "type": "view", "scope": "anonymous", "recipients": [{"email": "a@example.com"}]},
        [],
    ),
    ("schema: link type", "ONE_DRIVE_CREATE_LINK", {"item_id": ITEM_ID, "type": "public"}, []),
    ("schema: folder replace", "ONE_DRIVE_ONEDRIVE_CREATE_FOLDER", {"name": "x", "conflict_behavior": "replace"}, []),
    ("schema: missing item_id", "ONE_DRIVE_DOWNLOAD_FILE", {"file_name": "a.pdf"}, []),
]


@pytest.mark.parametrize(
    ("slug", "args", "answers"), [a[1:] for a in ARGUMENT_ERRORS], ids=[a[0] for a in ARGUMENT_ERRORS]
)
def test_argument_errors_are_invalid_arguments(slug, args, answers):
    h = harness(*answers)
    result = h.call(slug, args)
    assert result.successful is False
    assert result.error_code == "invalid_arguments", result.error
    assert len(h.session.calls) == len(answers)


def test_missing_credentials_are_unauthorized():
    h = harness()
    result = h.call("ONE_DRIVE_GET_DRIVE", {}, config={})
    assert result.error_code == "unauthorized"
    assert h.session.calls == []


def test_unknown_slug():
    assert harness().call("ONE_DRIVE_FORMAT_THE_DISK", {}).error_code == "unknown_tool"


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (OneDriveAPIError(429, "x", retry_after=1), "rate_limited"),
        (OneDriveAPIError(503, "x", retry_after=1), "rate_limited"),
        (OneDriveAPIError(0, "x", timed_out=True), "timeout"),
        (OneDriveAPIError(0, "x"), "upstream_error"),
        (OneDriveAPIError(410, "Gone"), "not_found"),
        (OneDriveAPIError(423, "Locked"), "conflict"),
        (OneDriveAPIError(422, "x"), "invalid_arguments"),
    ],
)
def test_error_code_mapping(error, code):
    assert error_code(error) == code


# --------------------------------------------------------------------------
# timeout_seconds: the whole call's budget
# --------------------------------------------------------------------------


class FakeClock:
    """Monotonic time that only moves when the client sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _budgeted(budget, *answers, **kwargs):
    clock = FakeClock()
    session = FakeSession(list(answers))
    client = OneDriveRestClient(
        TOKEN, session=session, budget=budget, sleep=clock.sleep, monotonic=clock, **kwargs  # type: ignore[arg-type]
    )
    return client, session, clock


class TestTimeBudget:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(5, 5.0), (2.5, 2.5), (None, None), (0, None), (-1, None), (True, None), ("10", None), (float("nan"), None), (float("inf"), None)],
    )
    def test_budget_from_config(self, value, expected):
        assert time_budget({**CONFIG, "timeout_seconds": value}) == expected

    @pytest.mark.parametrize(("budget", "timeout"), [(5, 5.0), (100, rest.DEFAULT_TIMEOUT), (None, rest.DEFAULT_TIMEOUT)])
    def test_request_timeout_is_capped_by_the_budget(self, budget, timeout):
        h = harness(response(200, {"id": "b!drive"}))
        config = {**CONFIG, "timeout_seconds": budget} if budget is not None else CONFIG
        h.call("ONE_DRIVE_GET_DRIVE", {}, config=config)
        assert h.session.last.timeout == pytest.approx(timeout, abs=0.5)

    def test_retry_sleep_never_outlasts_the_budget(self):
        h = harness(*_throttled(429, "20"))
        result = h.call(*GET, config={**CONFIG, "timeout_seconds": 10})
        assert h.sleeps == []
        assert len(h.session.calls) == 1
        assert result.error_code == "rate_limited"
        assert result.retry_after == 20.0

    def test_remaining_budget_shrinks_each_attempt(self):
        client, session, clock = _budgeted(5, *_throttled(429, "3"), response(200, {}))
        client.request("GET", "/me/drive")
        assert clock.sleeps == [3.0]
        assert [c.timeout for c in session.calls] == [5.0, 2.0]

    def test_a_retry_that_would_outlast_the_budget_fails_now(self):
        client, session, clock = _budgeted(5, *_throttled(429, "3", 2))
        with pytest.raises(OneDriveAPIError) as exc:
            client.request("GET", "/me/drive")
        assert exc.value.retry_after == 3.0
        assert clock.sleeps == [3.0]  # the second 3s wait would pass the 5s budget
        assert len(session.calls) == 2

    def test_spent_budget_times_out_without_a_request(self):
        client, session, clock = _budgeted(5)
        clock.now = 5.0
        with pytest.raises(OneDriveAPIError) as exc:
            client.request("GET", "/me/drive")
        assert exc.value.timed_out
        assert session.calls == []

    def test_request_timeout_names_the_seconds_not_the_url(self):
        h = harness(requests.ReadTimeout("HTTPSConnectionPool: /me/drive/root/search(q='project-x-secret')"))
        result = h.call("ONE_DRIVE_SEARCH_ITEMS", {"q": "project-x-secret"}, config={**CONFIG, "timeout_seconds": 7})
        assert result.error_code == "timeout"
        assert result.error == "Microsoft Graph did not answer within 7s (ReadTimeout)"

    def test_copy_monitor_stops_at_the_budget(self):
        in_progress = [response(202, {"status": "inProgress", "percentageComplete": 40}) for _ in range(10)]
        h = harness(response(202, headers={"Location": MONITOR_URL}), *in_progress)
        result = h.call("ONE_DRIVE_COPY_ITEM", {"item_id": ITEM_ID}, config={**CONFIG, "timeout_seconds": 0.5})
        # The sleeps are fake, so the budget never runs down; the 10s monitor cap ends it.
        assert result.successful
        assert result.data["status"] == "inProgress"
        assert sum(h.sleeps) <= 10.0


# --------------------------------------------------------------------------
# Concurrency
# --------------------------------------------------------------------------


class TestConcurrency:
    def test_each_call_gets_its_own_session_and_closes_it(self):
        made: list[FakeSession] = []
        lock = threading.Lock()

        def new_session():
            session = FakeSession([response(200, {"id": "b!drive"})])
            with lock:
                made.append(session)
            return session

        toolkit = OneDriveToolkit()
        with (
            mock.patch.object(rest.requests, "Session", side_effect=new_session),
            ThreadPoolExecutor(max_workers=8) as pool,
        ):
            results = list(pool.map(lambda _: toolkit.call_tool("ONE_DRIVE_GET_DRIVE", {}, CONFIG), range(16)))
        assert all(r.successful for r in results)
        assert len(made) == 16
        assert len({id(s) for s in made}) == 16
        assert all(s.closed and len(s.calls) == 1 for s in made)

    def test_fresh_instances_in_parallel_do_not_cross_talk(self):
        def run(i: int):
            item_id = f"01ITEM{i:028d}"
            h = harness(response(200, {"id": item_id, "name": f"f{i}.txt"}))
            result = h.call("ONE_DRIVE_GET_ITEM", {"item_id": item_id})
            return item_id, result.data["id"], h.session.last.path

        with ThreadPoolExecutor(max_workers=8) as pool:
            for item_id, returned, path in pool.map(run, range(1, 33)):
                assert returned == item_id
                assert path == f"/me/drive/items/{item_id}"


# --------------------------------------------------------------------------
# PR #38 review round 1
# --------------------------------------------------------------------------


class TestSleepLeavesRoomForTheRequest:
    """A throttling sleep must leave min(request timeout, 2s) of the budget
    for the retried request; otherwise the call fails at once as
    rate_limited with retry_after (never sleep the budget away)."""

    def test_wait_close_to_the_budget_fails_without_sleeping(self):
        client, session, clock = _budgeted(5, *_throttled(429, "4.5"), response(200, {}))
        with pytest.raises(OneDriveAPIError) as exc:
            client.request("GET", "/me/drive")
        assert exc.value.retry_after == 4.5 and not exc.value.timed_out
        assert clock.sleeps == []
        assert len(session.calls) == 1

    def test_wait_equal_to_the_budget_is_rate_limited_not_timeout(self):
        client, session, clock = _budgeted(5, *_throttled(429, "5"), response(200, {}))
        with pytest.raises(OneDriveAPIError) as exc:
            client.request("GET", "/me/drive")
        assert (exc.value.retry_after, exc.value.timed_out) == (5.0, False)
        assert clock.sleeps == []
        assert len(session.calls) == 1

    def test_through_call_tool(self):
        h = harness(*_throttled(429, "0.9"), response(200, {"id": "x"}))
        result = h.call(*GET, config={**CONFIG, "timeout_seconds": 1})
        assert (result.error_code, result.retry_after) == ("rate_limited", 1.0)
        assert h.sleeps == []
        assert len(h.session.calls) == 1

    def test_a_short_timeout_needs_only_its_own_length(self):
        # timeout 1s < the 2s floor: a 3s wait fits a 5s budget (3 + 1 <= 5).
        client, session, clock = _budgeted(5, *_throttled(429, "3"), response(200, {}), timeout=1.0)
        client.request("GET", "/me/drive")
        assert clock.sleeps == [3.0]
        assert [c.timeout for c in session.calls] == [1.0, 1.0]


@pytest.mark.parametrize("value", ["inf", "Infinity", "-inf", "1e400", "9" * 400, "nan", "NaN", "-5", "soon", ""])
def test_unusable_retry_after_falls_back_to_the_default_wait(value):
    h = harness(*_throttled(429, value, 3))
    result = h.call(*GET)
    assert (result.successful, result.error_code, result.retry_after) == (False, "rate_limited", 5.0)
    assert h.sleeps == [5.0, 5.0]
    assert "retry in about 5s" in result.error


def test_huge_finite_retry_after_is_capped():
    h = harness(*_throttled(429, "1e300"))
    result = h.call(*GET)
    assert (result.error_code, result.retry_after) == ("rate_limited", float(rest.MAX_RETRY_AFTER))
    assert h.sleeps == []


@pytest.mark.parametrize("value", ["inf", "nan", "-1", "later"])
def test_unusable_retry_after_on_a_503_is_an_outage(value):
    assert rest.throttle_wait(graph_error(503, "serviceNotAvailable", "x", headers={"Retry-After": value})) is None


def test_failure_never_raises_on_a_non_finite_retry_after():
    from aisquare_pipe_onedrive.toolkit import failure

    result = failure(OneDriveAPIError(429, "x", retry_after=float("inf")))
    assert (result.error_code, result.retry_after) == ("rate_limited", float(rest.MAX_RETRY_AFTER))


_DEEP_ARRAY = b"[" * 200_000 + b"]" * 200_000
_DEEP_OBJECT = b'{"error":' * 100_000 + b"1" + b"}" * 100_000


@pytest.mark.parametrize("body", [_DEEP_ARRAY, _DEEP_OBJECT], ids=["array", "object"])
@pytest.mark.parametrize(
    ("status", "headers", "code"),
    [(401, {}, "unauthorized"), (404, {}, "not_found"), (429, {"Retry-After": "2"}, "rate_limited")],
)
def test_any_error_body_keeps_its_status(body, status, headers, code):
    h = harness(*[response(status, content=body, headers=headers, content_type="application/json") for _ in range(3)])
    result = h.call(*GET)
    assert result.error_code == code
    assert "RecursionError" not in result.error
    assert result.data == {"status": status}


def test_a_2xx_body_too_deep_to_parse_is_not_json():
    h = harness(response(200, content=_DEEP_ARRAY, content_type="application/json"))
    result = h.call(*GET)
    assert result.error_code == "upstream_error"
    assert "not JSON" in result.error


# Page tokens: only a Graph nextLink for a drive root, of the same kind of
# listing (manager ruling on PR #38 m4). Anything else is refused unsent.
FOREIGN_PAGE_TOKENS = [
    "/me/messages?$top=5",
    "/me/",
    "/me/events",
    "/users/ana@contoso.com/messages",
    "/users/ana@contoso.com/drive/root/children",
    "/sites/contoso.sharepoint.com,1,2/drive/root/children",
    "/groups/g-1/drive/root/children",
    "/drives/b!d/items/01A/children/../../../../me/messages",
    "/drives//items/01A/children",
    "/me/drive/items/01A/permissions?$skiptoken=x",  # another listing's token
    "/me/drive/root/search(q='x')?$skiptoken=x",
    "https://graph.microsoft.com/v1.0/me/drive/root/children",
]


@pytest.mark.parametrize("token", FOREIGN_PAGE_TOKENS)
def test_folder_listing_refuses_any_other_page_token(token):
    h = harness()
    result = h.call("ONE_DRIVE_LIST_FOLDER_CHILDREN", {"page_token": token})
    assert result.error_code == "invalid_arguments"
    assert h.session.calls == []


@pytest.mark.parametrize(
    ("slug", "args", "token"),
    [
        ("ONE_DRIVE_LIST_FOLDER_CHILDREN", {}, "/drives/b!AbC-d_E/items/01ABC/children?$top=50&$skiptoken=X"),
        ("ONE_DRIVE_LIST_FOLDER_CHILDREN", {}, "/me/drive/root:/Documents:/children?$skiptoken=X"),
        ("ONE_DRIVE_SEARCH_ITEMS", {"q": "x"}, "/me/drive/root/search(q='x')?$skiptoken=X"),
        ("ONE_DRIVE_SEARCH_ITEMS", {"q": "x"}, "/drives/b!d/search(q='x')?$skiptoken=X"),
        ("ONE_DRIVE_GET_RECENT_ITEMS", {}, "/me/drive/recent?$skiptoken=X"),
        ("ONE_DRIVE_GET_RECENT_ITEMS", {}, "/drives/b!d/microsoft.graph.recent()?$skiptoken=X"),
        ("ONE_DRIVE_SEARCH_ITEMS", {"q": "x"}, "/me/drive/root/microsoft.graph.search(q='x')?$skiptoken=X"),
        ("ONE_DRIVE_GET_SHARED_ITEMS", {}, "/me/drive/sharedWithMe?$skiptoken=X"),
        ("ONE_DRIVE_GET_ITEM_PERMISSIONS", {"item_id": ITEM_ID}, "/drives/b!d/items/01A/permissions?$skiptoken=X"),
    ],
)
def test_own_kind_of_drive_token_is_accepted(slug, args, token):
    h = harness(response(200, {"value": []}))
    assert h.call(slug, {**args, "page_token": token}).successful
    assert h.session.last.path == token


@pytest.mark.parametrize(
    "next_link",
    ["/users/ana/drive/items/01A/children?$skiptoken=X", "/me/messages?$skiptoken=X"],
)
def test_a_next_link_the_tool_would_refuse_is_not_offered(next_link):
    h = harness(response(200, {"value": [], "@odata.nextLink": f"https://graph.microsoft.com/v1.0{next_link}"}))
    data = h.call("ONE_DRIVE_LIST_FOLDER_CHILDREN", {}).data
    assert (data["has_more"], data["next_page_token"]) == (True, None)
