"""Every Google Drive tool, end to end through ``call_tool`` with HTTP mocked
at the client boundary: request mapping, happy path, pagination hint, 404,
401 -> reconnect message, and rate-limit retry. One CASES row per tool."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

import pytest

from aisquare_pipe_googledrive import GoogleDriveToolkit
from aisquare_pipe_googledrive.formats import DOCUMENT
from aisquare_pipe_googledrive.rest import FOLDER_MIME
from aisquare_pipe_googledrive.tools import (
    DRIVE_FIELDS,
    FILE_FIELDS,
    FOLDER_FIELDS,
    METADATA_FIELDS,
    PERMISSION_FIELDS,
)

from .fakes import TOKEN, drive_error, file_meta, harness, rate_limited, response

ALL_DRIVES = {"corpora": "allDrives", "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
FILE_LIST_FIELDS = f"nextPageToken,incompleteSearch,files({FILE_FIELDS})"
TRASH_FIELDS = "id,name,mimeType,trashed,parents,webViewLink"
HINT_KEYS = ["has_more", "next_page_token"]


@dataclass(frozen=True)
class Case:
    slug: str
    args: dict[str, Any]
    method: str
    path: str
    params: dict[str, Any] | None = None
    json: Any = None
    reply: Any = field(default_factory=dict)
    #: Answers to the requests made before the one under test (metadata lookups).
    pre: tuple[Any, ...] = ()
    #: For list tools: the collection key in Drive's reply, and the page-token argument.
    collection: str | None = None
    token_arg: str = "pageToken"
    raw: bytes | None = None
    upload: bool = False
    body_contains: tuple[bytes, ...] = ()

    def answers(self, **kwargs: Any) -> list[Any]:
        if self.raw is not None:
            final = response(200, content=self.raw, content_type="application/octet-stream", **kwargs)
        else:
            final = response(200, self.reply, **kwargs)
        return [*(response(200, body) for body in self.pre), final]

    @property
    def id(self) -> str:
        return self.slug.removeprefix("GOOGLEDRIVE_")


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


FOLDER = "fold1234567890"
FILES_REPLY = {"kind": "drive#fileList", "incompleteSearch": False, "files": [file_meta(), file_meta("file456", "b.pdf", "application/pdf")]}

CASES = [
    # find / list
    Case(
        "GOOGLEDRIVE_FIND_FILE",
        {"q": "name contains 'roadmap'", "pageSize": 10},
        "GET",
        "/files",
        params={"q": "name contains 'roadmap'", "fields": FILE_LIST_FIELDS, "pageSize": 10, **ALL_DRIVES},
        reply=FILES_REPLY,
        collection="files",
    ),
    Case(
        "GOOGLEDRIVE_LIST_FILES",
        {"folderId": FOLDER, "orderBy": "modifiedTime desc"},
        "GET",
        "/files",
        params={
            "q": f"trashed = false and '{FOLDER}' in parents",
            "fields": FILE_LIST_FIELDS,
            "pageSize": 100,
            "orderBy": "modifiedTime desc",
            **ALL_DRIVES,
        },
        reply=FILES_REPLY,
        collection="files",
    ),
    Case(
        "GOOGLEDRIVE_FIND_FOLDER",
        {"name_contains": "Reports", "parent_folder_id": "root"},
        "GET",
        "/files",
        params={
            "q": f"mimeType = '{FOLDER_MIME}' and trashed = false and name contains 'Reports' and 'root' in parents",
            "fields": f"nextPageToken,incompleteSearch,files({FOLDER_FIELDS})",
            "pageSize": 100,
            **ALL_DRIVES,
        },
        reply={"files": [{"id": FOLDER, "name": "Reports"}]},
        collection="files",
        token_arg="page_token",
    ),
    Case(
        "GOOGLEDRIVE_GET_FILE_METADATA",
        {"fileId": "file123"},
        "GET",
        "/files/file123",
        params={"fields": METADATA_FIELDS, "supportsAllDrives": "true"},
        reply=file_meta(),
    ),
    Case(
        "GOOGLEDRIVE_DOWNLOAD_FILE",
        {"fileId": "file123"},
        "GET",
        "/files/file123",
        params={"alt": "media", "supportsAllDrives": "true"},
        pre=(file_meta(),),
        raw=b"hello",
    ),
    Case(
        "GOOGLEDRIVE_EXPORT_GOOGLE_WORKSPACE_FILE",
        {"fileId": "doc123", "mimeType": "text/plain"},
        "GET",
        "/files/doc123/export",
        params={"mimeType": "text/plain"},
        pre=(file_meta("doc123", "Roadmap", DOCUMENT, None),),
        raw=b"Roadmap text",
    ),
    Case(
        "GOOGLEDRIVE_GET_ABOUT",
        {},
        "GET",
        "/about",
        params={"fields": "user,storageQuota,maxUploadSize,canCreateDrives,appInstalled"},
        reply={"user": {"displayName": "Ada"}, "storageQuota": {"limit": "100", "usage": "10"}},
    ),
    Case(
        "GOOGLEDRIVE_LIST_SHARED_DRIVES",
        {},
        "GET",
        "/drives",
        params={"fields": f"nextPageToken,drives({DRIVE_FIELDS})", "pageSize": 100},
        reply={"drives": [{"id": "drive1", "name": "Marketing"}]},
        collection="drives",
    ),
    Case(
        "GOOGLEDRIVE_LIST_PERMISSIONS",
        {"fileId": "file123"},
        "GET",
        "/files/file123/permissions",
        params={"fields": f"nextPageToken,permissions({PERMISSION_FIELDS})", "supportsAllDrives": "true"},
        reply={"permissions": [{"id": "p1", "type": "user", "role": "owner"}]},
        collection="permissions",
    ),
    Case(
        "GOOGLEDRIVE_LIST_COMMENTS",
        {"fileId": "file123"},
        "GET",
        "/files/file123/comments",
        params={"fields": "*", "pageSize": 20, "includeDeleted": "false"},
        reply={"comments": [{"id": "c1", "content": "Nice"}]},
        collection="comments",
    ),
    # create
    Case(
        "GOOGLEDRIVE_CREATE_FOLDER",
        {"name": "Reports"},
        "POST",
        "/files",
        params={"supportsAllDrives": "true", "fields": FILE_FIELDS},
        json={"name": "Reports", "mimeType": FOLDER_MIME},
        reply={"id": FOLDER, "name": "Reports", "mimeType": FOLDER_MIME},
    ),
    Case(
        "GOOGLEDRIVE_UPLOAD_FILE",
        {
            "file_to_upload": {"name": "a.txt", "mimetype": "text/plain", "file_content_bytes": _b64(b"hello")},
            "folder_to_upload_to": FOLDER,
        },
        "POST",
        "/files",
        params={"supportsAllDrives": "true", "fields": FILE_FIELDS, "uploadType": "multipart"},
        reply=file_meta("new1", "a.txt"),
        upload=True,
        body_contains=(b'{"name": "a.txt", "parents": ["fold1234567890"]}', b"Content-Type: text/plain\r\n\r\nhello\r\n"),
    ),
    Case(
        "GOOGLEDRIVE_CREATE_FILE_FROM_TEXT",
        {"file_name": "notes.md", "text_content": "# hi", "mime_type": "text/markdown"},
        "POST",
        "/files",
        params={"supportsAllDrives": "true", "fields": FILE_FIELDS, "uploadType": "multipart"},
        reply=file_meta("new2", "notes.md", "text/markdown"),
        upload=True,
        body_contains=(b'{"name": "notes.md"}', b"Content-Type: text/markdown\r\n\r\n# hi\r\n"),
    ),
    # change
    Case(
        "GOOGLEDRIVE_EDIT_FILE",
        {"file_id": "file123", "content": "new text"},
        "PATCH",
        "/files/file123",
        params={"supportsAllDrives": "true", "fields": FILE_FIELDS, "uploadType": "multipart"},
        reply=file_meta(),
        upload=True,
        body_contains=(b"{}", b"Content-Type: text/plain\r\n\r\nnew text\r\n"),
    ),
    Case(
        "GOOGLEDRIVE_COPY_FILE",
        {"file_id": "file123", "new_title": "Copy of notes"},
        "POST",
        "/files/file123/copy",
        params={"supportsAllDrives": "true", "fields": FILE_FIELDS},
        json={"name": "Copy of notes"},
        reply=file_meta("copy1", "Copy of notes"),
    ),
    Case(
        "GOOGLEDRIVE_MOVE_FILE",
        {"file_id": "file123", "add_parents": "dest1234567", "remove_parents": "src1234567"},
        "PATCH",
        "/files/file123",
        params={"addParents": "dest1234567", "removeParents": "src1234567", "supportsAllDrives": "true", "fields": FILE_FIELDS},
        json={},
        reply=file_meta(parents=["dest1234567"]),
    ),
    Case(
        "GOOGLEDRIVE_UPDATE_FILE_PUT",
        {"fileId": "file123", "name": "New name", "starred": True},
        "PATCH",
        "/files/file123",
        params={"supportsAllDrives": "true", "fields": METADATA_FIELDS},
        json={"name": "New name", "starred": True},
        reply=file_meta(name="New name", starred=True),
    ),
    Case(
        "GOOGLEDRIVE_DELETE_FILE",
        {"fileId": "file123"},
        "PATCH",
        "/files/file123",
        params={"supportsAllDrives": "true", "fields": TRASH_FIELDS},
        json={"trashed": True},
        reply=file_meta(trashed=True),
    ),
    Case(
        "GOOGLEDRIVE_TRASH_FILE",
        {"file_id": "file123"},
        "PATCH",
        "/files/file123",
        params={"supportsAllDrives": "true", "fields": TRASH_FIELDS},
        json={"trashed": True},
        reply=file_meta(trashed=True),
    ),
    Case(
        "GOOGLEDRIVE_UNTRASH_FILE",
        {"file_id": "file123"},
        "PATCH",
        "/files/file123",
        params={"supportsAllDrives": "true", "fields": TRASH_FIELDS},
        json={"trashed": False},
        reply=file_meta(trashed=False),
    ),
    # share / comment
    Case(
        "GOOGLEDRIVE_ADD_FILE_SHARING_PREFERENCE",
        {"file_id": "file123", "role": "reader", "type": "user", "email_address": "ada@example.com"},
        "POST",
        "/files/file123/permissions",
        params={"supportsAllDrives": "true", "fields": PERMISSION_FIELDS},
        json={"role": "reader", "type": "user", "emailAddress": "ada@example.com"},
        reply={"id": "p2", "role": "reader", "type": "user"},
    ),
    Case(
        "GOOGLEDRIVE_CREATE_PERMISSION",
        {"file_id": "file123", "role": "writer", "type": "domain", "domain": "example.com", "send_notification_email": False},
        "POST",
        "/files/file123/permissions",
        params={"sendNotificationEmail": "false", "supportsAllDrives": "true", "fields": PERMISSION_FIELDS},
        json={"role": "writer", "type": "domain", "domain": "example.com"},
        reply={"id": "p3", "role": "writer", "type": "domain"},
    ),
    Case(
        "GOOGLEDRIVE_CREATE_COMMENT",
        {"file_id": "file123", "content": "Looks good"},
        "POST",
        "/files/file123/comments",
        params={"fields": "*"},
        json={"content": "Looks good"},
        reply={"id": "c2", "content": "Looks good"},
    ),
]
PAGED = [c for c in CASES if c.collection]
IDS = [c.id for c in CASES]


def test_cases_cover_every_tool_exactly_once():
    assert sorted(c.slug for c in CASES) == sorted(t.slug for t in GoogleDriveToolkit().tools())


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_happy_path(case: Case):
    h = harness(*case.answers())
    result = h.call(case.slug, case.args)
    assert result.successful, result.error
    assert (result.error, result.error_code, result.retry_after) == (None, None, None)
    assert len(h.session.calls) == len(case.pre) + 1
    call = h.session.last
    assert (call.method, call.path, call.upload) == (case.method, case.path, case.upload)
    assert call.params == case.params
    assert call.json == case.json
    for chunk in case.body_contains:
        assert chunk in call.data
    assert all(c.headers["Authorization"] == f"Bearer {TOKEN}" for c in h.session.calls)
    assert isinstance(result.data, dict)
    assert "kind" not in result.data
    if case.collection:
        assert list(result.data)[:2] == HINT_KEYS
        assert result.data["has_more"] is False
        assert result.data["next_page_token"] is None
        assert result.data["items"] == [
            {k: v for k, v in item.items() if k != "kind"} for item in case.reply[case.collection]
        ]
    if case.raw is not None:
        (envelope,) = result.files
        assert envelope.data == case.raw
        assert result.data["size"] == len(case.raw)


@pytest.mark.parametrize("case", PAGED, ids=[c.id for c in PAGED])
def test_pagination_hint(case: Case):
    h = harness(response(200, {**case.reply, "nextPageToken": "tok2"}))
    result = h.call(case.slug, case.args)
    assert result.successful, result.error
    assert {k: result.data[k] for k in HINT_KEYS} == {"has_more": True, "next_page_token": "tok2"}


@pytest.mark.parametrize("case", PAGED, ids=[c.id for c in PAGED])
def test_next_page_token_is_sent(case: Case):
    h = harness(response(200, case.reply))
    h.call(case.slug, {**case.args, case.token_arg: "tok2"})
    assert h.session.last.params is not None
    assert h.session.last.params["pageToken"] == "tok2"


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_not_found(case: Case):
    h = harness(drive_error(404, "File not found: file123.", "notFound"))
    result = h.call(case.slug, case.args)
    assert result.successful is False
    assert result.error_code == "not_found"
    assert result.error.startswith("404: File not found: file123.")
    assert "cannot see it" in result.error
    assert result.data == {"status": 404, "reason": "notFound"}


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_unauthorized_says_reconnect(case: Case):
    h = harness(drive_error(401, "Request had invalid authentication credentials.", "authError"))
    result = h.call(case.slug, case.args)
    assert result.successful is False
    assert result.error_code == "unauthorized"
    assert result.error.startswith("401: Request had invalid authentication credentials.")
    assert "must be reconnected" in result.error
    assert result.data == {"status": 401, "reason": "authError"}


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_rate_limit_is_retried(case: Case):
    h = harness(*rate_limited(1), *case.answers())
    result = h.call(case.slug, case.args)
    assert result.successful, result.error
    assert result.error_code is None and result.retry_after is None
    assert h.sleeps == [1.0]
    assert len(h.session.calls) == len(case.pre) + 2
    assert h.session.calls[0].path == h.session.calls[1].path


@pytest.mark.parametrize("case", CASES[:3], ids=IDS[:3])
def test_rate_limit_that_persists_reports_retry_after(case: Case):
    h = harness(*rate_limited(4))
    result = h.call(case.slug, case.args)
    assert result.successful is False
    assert result.error_code == "rate_limited"
    assert result.retry_after == 8.0
    assert result.error.startswith("403: User Rate Limit Exceeded")
    assert "retry in about 8s" in result.error
    assert result.data == {"status": 403, "reason": "userRateLimitExceeded"}
    assert h.sleeps == [1.0, 2.0, 4.0]
