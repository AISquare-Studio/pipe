"""Every OneDrive tool, end to end through ``call_tool`` with HTTP mocked at
the client boundary: request mapping, happy path, pagination hint, 404,
401 -> reconnect message, and throttling retry. One CASES row per tool."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from aisquare_pipe_onedrive import OneDriveToolkit

from .fakes import (
    MONITOR_URL,
    file_item,
    folder_item,
    graph_error,
    harness,
    listing,
    response,
)

ITEM_ID = "01BYE5RZ6QN3ZWBTUFOFD3GSPGOHDJD36K"
ITEM = f"/me/drive/items/{ITEM_ID}"
NEXT = f"/me/drive/items/{ITEM_ID}/children?$top=50&$skiptoken=UGFnZWQ9VFJVRSZwX1NvcnRCZWhhdmlvcj0x"
HINT_KEYS = ["has_more", "next_page_token"]


@dataclass(frozen=True)
class Case:
    slug: str
    args: dict[str, Any]
    method: str
    path: str
    params: dict[str, Any] | None = None
    json: Any = None
    data: Any = None
    replies: tuple[Any, ...] = field(default_factory=tuple)
    #: (method, path) of each request after the first.
    then: tuple[tuple[str, str], ...] = ()
    paged: bool = False

    def ok_responses(self, **kwargs: Any) -> list[Any]:
        if not self.replies:
            return [response(200, {}, **kwargs)]
        first, *rest = self.replies
        if kwargs:
            first = response(first.status_code, first.json(), **kwargs)
        return [first, *rest]

    @property
    def id(self) -> str:
        return self.slug.removeprefix("ONE_DRIVE_")


CASES = [
    # ---------------------------------------------------------------- reads
    Case(
        "ONE_DRIVE_SEARCH_ITEMS",
        {"q": "quarterly report"},
        "GET",
        "/me/drive/root/search(q='quarterly%20report')",
        params={"$top": 50},
        replies=(response(200, listing([file_item(), folder_item()])),),
        paged=True,
    ),
    Case(
        "ONE_DRIVE_DOWNLOAD_FILE",
        {"item_id": ITEM_ID, "file_name": "report.pdf"},
        "GET",
        ITEM,
        replies=(response(200, file_item(item_id=ITEM_ID)), response(200, content=b"%PDF-1.7 q3", content_type="application/pdf")),
        then=(("GET", f"{ITEM}/content"),),
    ),
    Case(
        "ONE_DRIVE_GET_ITEM",
        {"item_id": ITEM_ID, "select_fields": ["id", "name", "size"], "expand_relations": ["children"]},
        "GET",
        ITEM,
        params={"$select": "id,name,size", "$expand": "children"},
        replies=(response(200, file_item(item_id=ITEM_ID)),),
    ),
    Case(
        "ONE_DRIVE_GET_ITEM_BY_PATH",
        {"item_path": "Documents/Reports/q3 plan.pdf"},
        "GET",
        "/me/drive/root:/Documents/Reports/q3%20plan.pdf:",
        replies=(response(200, file_item("q3 plan.pdf")),),
    ),
    Case(
        "ONE_DRIVE_LIST_FOLDER_CHILDREN",
        {"folder_path": "/Documents", "orderby": "name"},
        "GET",
        "/me/drive/root:/Documents:/children",
        params={"$top": 50, "$orderby": "name"},
        replies=(response(200, listing([folder_item("Reports"), file_item()])),),
        paged=True,
    ),
    Case(
        "ONE_DRIVE_GET_DRIVE",
        {},
        "GET",
        "/me/drive",
        replies=(
            response(
                200,
                {"id": "b!drive123", "driveType": "business", "quota": {"total": 1099511627776, "used": 52428800, "state": "normal"}},
            ),
        ),
    ),
    Case(
        "ONE_DRIVE_GET_RECENT_ITEMS",
        {"top": 10},
        "GET",
        "/me/drive/recent",
        params={"$top": 10},
        replies=(response(200, listing([file_item()])),),
        paged=True,
    ),
    Case(
        "ONE_DRIVE_GET_SHARED_ITEMS",
        {"allow_external": True},
        "GET",
        "/me/drive/sharedWithMe",
        params={"allowexternal": "true"},
        replies=(response(200, listing([{"id": "01SHARED", "name": "plan.docx", "remoteItem": {"id": "R1", "parentReference": {"driveId": "b!other"}}}])),),
        paged=True,
    ),
    Case(
        "ONE_DRIVE_GET_ITEM_PERMISSIONS",
        {"item_id": ITEM_ID},
        "GET",
        f"{ITEM}/permissions",
        replies=(response(200, listing([{"id": "p1", "roles": ["owner"], "grantedToV2": {"user": {"displayName": "Ana"}}}])),),
        paged=True,
    ),
    # --------------------------------------------------------------- writes
    Case(
        "ONE_DRIVE_ONEDRIVE_UPLOAD_FILE",
        {"file_name": "notes.txt", "content": "hello", "folder": "/Documents"},
        "PUT",
        "/me/drive/root:/Documents/notes.txt:/content",
        params={"@microsoft.graph.conflictBehavior": "rename"},
        data=b"hello",
        replies=(response(201, file_item("notes.txt")),),
    ),
    Case(
        "ONE_DRIVE_ONEDRIVE_CREATE_FOLDER",
        {"name": "Q4", "parent_folder": "/Projects"},
        "POST",
        "/me/drive/root:/Projects:/children",
        json={"name": "Q4", "folder": {}, "@microsoft.graph.conflictBehavior": "rename"},
        replies=(response(201, folder_item("Q4")),),
    ),
    Case(
        "ONE_DRIVE_MOVE_ITEM",
        {"itemId": ITEM_ID, "parentReference": {"id": "01DESTFOLDER"}, "name": "renamed.pdf"},
        "PATCH",
        ITEM,
        json={"parentReference": {"id": "01DESTFOLDER"}, "name": "renamed.pdf"},
        replies=(response(200, file_item("renamed.pdf")),),
    ),
    Case(
        "ONE_DRIVE_COPY_ITEM",
        {"item_id": ITEM_ID, "parent_reference": {"id": "01DESTFOLDER", "driveId": "b!drive123"}, "name": "copy.pdf"},
        "POST",
        f"{ITEM}/copy",
        json={"parentReference": {"id": "01DESTFOLDER", "driveId": "b!drive123"}, "name": "copy.pdf"},
        replies=(
            response(202, headers={"Location": MONITOR_URL}),
            response(200, {"status": "completed", "percentageComplete": 100, "resourceId": "01NEWCOPY"}),
        ),
        then=(("GET", MONITOR_URL),),
    ),
    Case(
        "ONE_DRIVE_UPDATE_DRIVE_ITEM_METADATA",
        {"item_id": ITEM_ID, "name": "final.pdf"},
        "PATCH",
        ITEM,
        json={"name": "final.pdf"},
        replies=(response(200, file_item("final.pdf")),),
    ),
    Case(
        "ONE_DRIVE_DELETE_ITEM",
        {"item_id": ITEM_ID},
        "DELETE",
        ITEM,
        replies=(response(204),),
    ),
    Case(
        "ONE_DRIVE_CREATE_LINK",
        {"item_id": ITEM_ID, "type": "view", "scope": "organization"},
        "POST",
        f"{ITEM}/createLink",
        json={"type": "view", "scope": "organization"},
        replies=(
            response(
                201,
                {"id": "perm1", "roles": ["read"], "link": {"type": "view", "scope": "organization", "webUrl": "https://1drv.ms/b/s!AkYb"}},
            ),
        ),
    ),
]
PAGED = [c for c in CASES if c.paged]
IDS = [c.id for c in CASES]


def test_cases_cover_every_tool_exactly_once():
    assert sorted(c.slug for c in CASES) == sorted(t.slug for t in OneDriveToolkit().tools())


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_happy_path(case: Case):
    h = harness(*case.ok_responses())
    result = h.call(case.slug, case.args)
    assert result.successful, result.error
    assert (result.error, result.error_code, result.retry_after) == (None, None, None)
    first = h.session.calls[0]
    assert (first.method, first.path) == (case.method, case.path)
    assert first.params == case.params
    assert first.json == case.json
    assert first.data == case.data
    assert first.headers["Authorization"] == "Bearer EwBIA8l6BAAUs3cr3tT0kenValue123"
    assert [(c.method, c.path) for c in h.session.calls[1:]] == list(case.then)
    assert isinstance(result.data, dict)
    if case.paged:
        assert list(result.data) == [*HINT_KEYS, "items"]
        assert result.data["has_more"] is False
        assert result.data["next_page_token"] is None
        assert result.data["items"]


@pytest.mark.parametrize("case", PAGED, ids=[c.id for c in PAGED])
def test_pagination_hint(case: Case):
    body = case.replies[0].json()
    h = harness(response(200, {**body, "@odata.nextLink": f"https://graph.microsoft.com/v1.0{NEXT}"}))
    result = h.call(case.slug, case.args)
    assert result.successful, result.error
    assert {k: result.data[k] for k in HINT_KEYS} == {"has_more": True, "next_page_token": NEXT}


@pytest.mark.parametrize("case", PAGED, ids=[c.id for c in PAGED])
def test_page_token_fetches_that_page(case: Case):
    h = harness(response(200, listing([file_item("page2.pdf")])))
    result = h.call(case.slug, {**case.args, "page_token": NEXT})
    assert result.successful, result.error
    assert h.session.last.path == NEXT
    assert h.session.last.params is None  # the token carries the query options
    assert result.data["items"][0]["name"] == "page2.pdf"


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_not_found(case: Case):
    h = harness(graph_error(404, "itemNotFound", "The resource could not be found."))
    result = h.call(case.slug, case.args)
    assert result.successful is False
    assert result.error_code == "not_found"
    assert result.error.startswith("404: itemNotFound: The resource could not be found.")
    assert "cannot access" in result.error
    assert result.data == {"status": 404}


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_unauthorized_says_reconnect(case: Case):
    h = harness(graph_error(401, "InvalidAuthenticationToken", "Access token has expired or is not yet valid."))
    result = h.call(case.slug, case.args)
    assert result.successful is False
    assert result.error_code == "unauthorized"
    assert result.error.startswith("401: InvalidAuthenticationToken: Access token has expired")
    assert "must be reconnected" in result.error
    assert result.data == {"status": 401}


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_throttling_is_retried(case: Case):
    throttled = graph_error(429, "activityLimitReached", "The request has been throttled", headers={"Retry-After": "2"})
    h = harness(throttled, *case.ok_responses())
    result = h.call(case.slug, case.args)
    assert result.successful, result.error
    assert result.error_code is None and result.retry_after is None
    assert h.sleeps == [2.0]
    assert h.session.calls[0].path == h.session.calls[1].path == case.path


@pytest.mark.parametrize("case", CASES[:3], ids=IDS[:3])
def test_throttling_that_persists_reports_retry_after(case: Case):
    throttled = [
        graph_error(429, "activityLimitReached", "The request has been throttled", headers={"Retry-After": "2"})
        for _ in range(3)
    ]
    h = harness(*throttled)
    result = h.call(case.slug, case.args)
    assert result.successful is False
    assert result.error_code == "rate_limited"
    assert result.retry_after == 2.0
    assert result.error.startswith("429: activityLimitReached: The request has been throttled")
    assert "retry in about 2s" in result.error
    assert result.data == {"status": 429}
    assert h.sleeps == [2.0, 2.0]
