"""OneDriveToolkit behaviour beyond the per-tool table: catalog contract,
result shaping, downloads, uploads, async copy, drive routing, argument
forms, secrecy, and tool ranking for real requests."""

from __future__ import annotations

import base64
import logging

import pytest
from aisquare.pipe import AuthType, get_toolkit

from aisquare_pipe_onedrive import MICROSOFT_OAUTH, OneDriveToolkit, tools
from aisquare_pipe_onedrive.rest import OneDriveAPIError
from aisquare_pipe_onedrive.toolkit import describe_error

from .fakes import (
    CONFIG,
    MONITOR_URL,
    TOKEN,
    UPLOAD_URL,
    file_item,
    folder_item,
    graph_error,
    harness,
    listing,
    response,
)

ITEM_ID = "01BYE5RZ6QN3ZWBTUFOFD3GSPGOHDJD36K"
PERSONAL_ID = "D4648F06C91D9D3D!54927"

READ_SLUGS = {
    "ONE_DRIVE_SEARCH_ITEMS",
    "ONE_DRIVE_DOWNLOAD_FILE",
    "ONE_DRIVE_GET_ITEM",
    "ONE_DRIVE_GET_ITEM_BY_PATH",
    "ONE_DRIVE_LIST_FOLDER_CHILDREN",
    "ONE_DRIVE_GET_DRIVE",
    "ONE_DRIVE_GET_RECENT_ITEMS",
    "ONE_DRIVE_GET_SHARED_ITEMS",
    "ONE_DRIVE_GET_ITEM_PERMISSIONS",
}
WRITE_SLUGS = {
    "ONE_DRIVE_ONEDRIVE_UPLOAD_FILE",
    "ONE_DRIVE_ONEDRIVE_CREATE_FOLDER",
    "ONE_DRIVE_MOVE_ITEM",
    "ONE_DRIVE_COPY_ITEM",
    "ONE_DRIVE_UPDATE_DRIVE_ITEM_METADATA",
    "ONE_DRIVE_DELETE_ITEM",
    "ONE_DRIVE_CREATE_LINK",
}
#: Every slug above, as listed in Composio's one_drive toolkit
#: (docs.composio.dev/toolkits/one_drive, version 20261013_00).
COMPOSIO_ONE_DRIVE_SLUGS = READ_SLUGS | WRITE_SLUGS


class TestCatalog:
    def test_registered_as_the_onedrive_toolkit(self):
        assert get_toolkit("onedrive") is OneDriveToolkit

    def test_identity(self):
        toolkit = OneDriveToolkit()
        assert (toolkit.name, toolkit.version, toolkit.slug_prefix) == ("onedrive", "0.3.0", "ONE_DRIVE_")
        assert toolkit.auth_type is AuthType.OAUTH2
        assert toolkit.oauth is MICROSOFT_OAUTH

    def test_composio_slugs_exactly(self):
        assert {t.slug for t in OneDriveToolkit().tools()} == COMPOSIO_ONE_DRIVE_SLUGS

    def test_read_only_and_destructive_flags(self):
        tools_by_slug = {t.slug: t for t in OneDriveToolkit().tools()}
        assert {s for s, t in tools_by_slug.items() if t.read_only} == READ_SLUGS
        assert {s for s, t in tools_by_slug.items() if t.destructive} == {
            "ONE_DRIVE_DELETE_ITEM",
            "ONE_DRIVE_ONEDRIVE_UPLOAD_FILE",
            "ONE_DRIVE_COPY_ITEM",
        }

    def test_be_argument_names(self):
        tools_by_slug = {t.slug: t for t in OneDriveToolkit().tools()}
        search = tools_by_slug["ONE_DRIVE_SEARCH_ITEMS"].input_schema
        assert {"q", "top"} <= set(search["properties"]) and search["required"] == ["q"]
        download = tools_by_slug["ONE_DRIVE_DOWNLOAD_FILE"].input_schema
        assert {"item_id", "file_name"} <= set(download["properties"])

    def test_validate_config(self):
        toolkit = OneDriveToolkit()
        assert toolkit.validate_config({}) is False
        assert toolkit.validate_config({"access_token": "  "}) is False
        assert toolkit.validate_config({"client_id": "c", "client_secret": "s", "tenant_id": "t"}) is False
        assert toolkit.validate_config(CONFIG) is True

    def test_no_credentials_means_no_request(self):
        h = harness()
        result = h.call("ONE_DRIVE_GET_DRIVE", {}, config={})
        assert result.successful is False
        assert "not connected" in result.error
        assert h.session.calls == []

    @pytest.mark.parametrize(
        ("query", "slug"),
        [
            ("find my quarterly report", "ONE_DRIVE_SEARCH_ITEMS"),
            ("download the budget file", "ONE_DRIVE_DOWNLOAD_FILE"),
            ("what is in my Documents folder", "ONE_DRIVE_LIST_FOLDER_CHILDREN"),
            ("how much storage space do I have left", "ONE_DRIVE_GET_DRIVE"),
            ("share this file with a link", "ONE_DRIVE_CREATE_LINK"),
            ("upload these notes", "ONE_DRIVE_ONEDRIVE_UPLOAD_FILE"),
            ("files shared with me", "ONE_DRIVE_GET_SHARED_ITEMS"),
            ("rename the report", "ONE_DRIVE_UPDATE_DRIVE_ITEM_METADATA"),
        ],
    )
    def test_ranking_puts_the_right_tool_first(self, query, slug):
        assert OneDriveToolkit().search_tools(query, limit=3)[0].slug == slug


class TestResultShape:
    def test_annotations_and_download_urls_are_dropped(self):
        h = harness(response(200, listing([file_item()])))
        data = h.call("ONE_DRIVE_LIST_FOLDER_CHILDREN", {}).data
        (item,) = data["items"]
        assert "@microsoft.graph.downloadUrl" not in item
        assert "1drv.com" not in str(data)
        assert item["file"]["mimeType"] == "application/pdf"

    def test_object_results_are_cleaned_too(self):
        h = harness(response(200, {"@odata.context": "x", **file_item(), "children": [file_item("b.pdf")]}))
        data = h.call("ONE_DRIVE_GET_ITEM", {"item_id": ITEM_ID, "expand_relations": ["children"]}).data
        assert not [k for k in data if k.startswith("@")]
        assert "@microsoft.graph.downloadUrl" not in data["children"][0]

    def test_empty_listing(self):
        h = harness(response(200, {"value": []}))
        assert h.call("ONE_DRIVE_GET_RECENT_ITEMS", {}).data == {"has_more": False, "next_page_token": None, "items": []}

    def test_next_link_off_the_api_base_gives_no_token(self):
        h = harness(response(200, listing([file_item()], next_link="https://other.example/v1.0/me/drive/x")))
        data = h.call("ONE_DRIVE_LIST_FOLDER_CHILDREN", {}).data
        assert (data["has_more"], data["next_page_token"]) == (True, None)

    @pytest.mark.parametrize(("top", "sent"), [(500, 200), (0, 1), (10.0, 10)])
    def test_top_is_clamped(self, top, sent):
        h = harness(response(200, listing([])))
        h.call("ONE_DRIVE_LIST_FOLDER_CHILDREN", {"top": top})
        assert h.session.last.params["$top"] == sent


class TestDownload:
    def _download(self, args=None, meta=None, content=b"%PDF-1.7", **kw):
        h = harness(response(200, meta or file_item(item_id=ITEM_ID)), response(200, content=content, **kw))
        return h, h.call("ONE_DRIVE_DOWNLOAD_FILE", {"item_id": ITEM_ID, **(args or {})})

    def test_bytes_travel_in_files(self):
        h, result = self._download(content=b"\x00\x01binary\xff", content_type="application/pdf")
        (envelope,) = result.files
        assert envelope.data == b"\x00\x01binary\xff"
        assert envelope.content_type == "application/pdf"
        assert envelope.metadata["filename"] == "report.pdf"
        assert envelope.metadata["item_id"] == ITEM_ID
        assert envelope.metadata["drive_id"] == "b!drive123"
        assert result.data == {
            "id": ITEM_ID,
            "name": "report.pdf",
            "mimetype": "application/pdf",
            "size": 9,
            "webUrl": file_item()["webUrl"],
            "lastModifiedDateTime": "2026-09-30T12:00:00Z",
        }
        assert h.session.last.headers["Accept"] == "*/*"

    def test_small_text_is_inline(self):
        meta = file_item("notes.md", item_id=ITEM_ID, file={"mimeType": "text/markdown"})
        _, result = self._download(meta=meta, content="# Notes ✓\n".encode())
        assert result.data["content"] == "# Notes ✓\n"

    def test_large_text_is_not_inline(self):
        meta = file_item("big.txt", item_id=ITEM_ID, file={"mimeType": "text/plain"})
        _, result = self._download(meta=meta, content=b"a" * (tools.TEXT_INLINE_MAX + 1))
        assert "content" not in result.data
        assert result.files[0].data == b"a" * (tools.TEXT_INLINE_MAX + 1)

    def test_file_name_names_the_download(self):
        _, result = self._download(args={"file_name": "Q3 board pack.pdf"})
        assert result.data["name"] == result.files[0].metadata["filename"] == "Q3 board pack.pdf"

    def test_pdf_conversion(self):
        meta = file_item("plan.docx", item_id=ITEM_ID, file={"mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"})
        h, result = self._download(args={"format": "pdf"}, meta=meta)
        assert h.session.last.path == f"/me/drive/items/{ITEM_ID}/content"
        assert h.session.last.params == {"format": "pdf"}
        assert result.data["name"] == "plan.pdf"
        assert result.data["mimetype"] == result.files[0].content_type == "application/pdf"

    def test_mimetype_falls_back_to_the_name(self):
        meta = {"id": ITEM_ID, "name": "data.csv", "size": 3, "file": {}}
        _, result = self._download(meta=meta, content=b"a,b")
        assert result.data["mimetype"] == "text/csv"

    def test_shared_item_reads_from_the_owners_drive(self):
        meta = {
            "id": "01LOCALREF",
            "name": "plan.docx",
            "remoteItem": {
                "id": "REMOTE!123",
                "size": 10,
                "file": {"mimeType": "application/msword"},
                "parentReference": {"driveId": "b!ownerdrive"},
            },
        }
        h, result = self._download(meta=meta, content=b"0123456789")
        assert h.session.last.path == "/drives/b!ownerdrive/items/REMOTE!123/content"
        assert result.data["name"] == "plan.docx"
        assert result.data["mimetype"] == "application/msword"


class TestUpload:
    def test_base64_content(self):
        h = harness(response(201, file_item("logo.png")))
        raw = b"\x89PNG\r\n\x1a\n\x00"
        h.call("ONE_DRIVE_ONEDRIVE_UPLOAD_FILE", {"file_name": "logo.png", "content": base64.b64encode(raw).decode(), "encoding": "base64"})
        assert h.session.last.data == raw
        assert h.session.last.path == "/me/drive/root:/logo.png:/content"

    @pytest.mark.parametrize(
        ("folder", "path"),
        [
            (ITEM_ID, f"/me/drive/items/{ITEM_ID}:/a.txt:/content"),
            (PERSONAL_ID, f"/me/drive/items/{PERSONAL_ID}:/a.txt:/content"),
            ("/Q1!Reports", "/me/drive/root:/Q1!Reports/a.txt:/content"),
            ("Documents/Reports", "/me/drive/root:/Documents/Reports/a.txt:/content"),
            ("/", "/me/drive/root:/a.txt:/content"),
            ("/drive/root:/Documents", "/me/drive/root:/Documents/a.txt:/content"),
        ],
    )
    def test_folder_by_id_or_path(self, folder, path):
        h = harness(response(201, file_item("a.txt")))
        h.call("ONE_DRIVE_ONEDRIVE_UPLOAD_FILE", {"file_name": "a.txt", "content": "x", "folder": folder})
        assert h.session.last.path == path

    def test_description_is_set_after_a_simple_upload(self):
        h = harness(response(201, file_item("a.txt", item_id=ITEM_ID)), response(200, file_item("a.txt", item_id=ITEM_ID, description="d")))
        result = h.call("ONE_DRIVE_ONEDRIVE_UPLOAD_FILE", {"file_name": "a.txt", "content": "x", "description": "d"})
        assert result.successful
        assert (h.session.last.method, h.session.last.path, h.session.last.json) == ("PATCH", f"/me/drive/items/{ITEM_ID}", {"description": "d"})

    def test_large_file_uses_an_upload_session(self, monkeypatch):
        monkeypatch.setattr(tools, "SIMPLE_UPLOAD_LIMIT", 10)
        monkeypatch.setattr(tools, "CHUNK_SIZE", 8)
        raw = b"0123456789abcdefghij"  # 20 bytes -> chunks of 8, 8, 4
        h = harness(
            response(200, {"uploadUrl": UPLOAD_URL, "expirationDateTime": "2026-10-17T00:00:00Z"}),
            response(202, {"nextExpectedRanges": ["8-"]}),
            response(202, {"nextExpectedRanges": ["16-"]}),
            response(201, file_item("big.bin")),
        )
        result = h.call(
            "ONE_DRIVE_ONEDRIVE_UPLOAD_FILE",
            {"file_name": "big.bin", "content": base64.b64encode(raw).decode(), "encoding": "base64", "folder": "/Backups", "conflict_behavior": "replace", "description": "nightly"},
        )
        assert result.successful, result.error
        assert result.data["name"] == "big.bin"
        session_call, *chunks = h.session.calls
        assert (session_call.method, session_call.path) == ("POST", "/me/drive/root:/Backups/big.bin:/createUploadSession")
        assert session_call.json == {"item": {"@microsoft.graph.conflictBehavior": "replace", "description": "nightly"}}
        assert [c.url for c in chunks] == [UPLOAD_URL] * 3
        assert [c.headers["Content-Range"] for c in chunks] == ["bytes 0-7/20", "bytes 8-15/20", "bytes 16-19/20"]
        assert b"".join(c.data for c in chunks) == raw
        # The upload URL is pre-authenticated: the bearer token never goes there.
        assert all("Authorization" not in c.headers for c in chunks)

    def test_failed_chunk_cancels_the_session(self, monkeypatch):
        monkeypatch.setattr(tools, "SIMPLE_UPLOAD_LIMIT", 4)
        h = harness(
            response(200, {"uploadUrl": UPLOAD_URL}),
            graph_error(416, "invalidRange", "bad range"),
            response(204),
        )
        result = h.call("ONE_DRIVE_ONEDRIVE_UPLOAD_FILE", {"file_name": "a.bin", "content": "0123456789"})
        assert result.successful is False
        assert (h.session.last.method, h.session.last.url) == ("DELETE", UPLOAD_URL)
        assert "Authorization" not in h.session.last.headers

    def test_session_without_upload_url_fails(self, monkeypatch):
        monkeypatch.setattr(tools, "SIMPLE_UPLOAD_LIMIT", 4)
        h = harness(response(200, {"uploadUrl": "http://insecure.example/x"}))
        result = h.call("ONE_DRIVE_ONEDRIVE_UPLOAD_FILE", {"file_name": "a.bin", "content": "0123456789"})
        assert result.successful is False
        assert result.error_code == "upstream_error"
        assert len(h.session.calls) == 1


class TestCopy:
    def test_monitor_is_polled_until_complete(self):
        h = harness(
            response(202, headers={"Location": MONITOR_URL}),
            response(202, {"status": "inProgress", "percentageComplete": 10.0}),
            response(202, {"status": "inProgress", "percentageComplete": 60.0}),
            response(200, {"status": "completed", "resourceId": "01NEWCOPY"}),
        )
        result = h.call("ONE_DRIVE_COPY_ITEM", {"item_id": ITEM_ID, "conflict_behavior": "rename"})
        assert result.data == {"status": "completed", "percentageComplete": 100.0, "resourceId": "01NEWCOPY"}
        assert h.session.calls[0].params == {"@microsoft.graph.conflictBehavior": "rename"}
        assert h.sleeps == [0.5, 1.0]
        monitor_calls = h.session.calls[1:]
        assert all(c.url == MONITOR_URL and "Authorization" not in c.headers for c in monitor_calls)
        assert all(c.allow_redirects is False for c in monitor_calls)
        assert MONITOR_URL not in str(result.data)

    def test_long_copy_reports_in_progress(self):
        polls = [response(202, {"status": "inProgress", "percentageComplete": 5}) for _ in range(20)]
        h = harness(response(202, headers={"Location": MONITOR_URL}), *polls)
        result = h.call("ONE_DRIVE_COPY_ITEM", {"item_id": ITEM_ID})
        assert result.successful
        assert result.data["status"] == "inProgress"
        assert result.data["percentageComplete"] == 5.0
        assert sum(h.sleeps) <= tools.COPY_MONITOR_WAIT

    def test_redirecting_monitor_means_complete(self):
        h = harness(response(202, headers={"Location": MONITOR_URL}), response(303, headers={"Location": "https://graph.microsoft.com/v1.0/drives/x/items/y"}))
        assert h.call("ONE_DRIVE_COPY_ITEM", {"item_id": ITEM_ID}).data["status"] == "completed"

    def test_no_monitor_url_is_still_accepted(self):
        h = harness(response(202))
        result = h.call("ONE_DRIVE_COPY_ITEM", {"item_id": ITEM_ID})
        assert result.successful and result.data["status"] == "inProgress"
        assert len(h.session.calls) == 1

    def test_monitor_network_error_does_not_fail_an_accepted_copy(self):
        import requests

        h = harness(response(202, headers={"Location": MONITOR_URL}), requests.ConnectionError("x"))
        result = h.call("ONE_DRIVE_COPY_ITEM", {"item_id": ITEM_ID})
        assert result.successful and result.data["status"] == "inProgress"


class TestRouting:
    @pytest.mark.parametrize(
        ("extra", "prefix"),
        [
            ({}, "/me/drive"),
            ({"user_id": "me"}, "/me/drive"),
            ({"user_id": "ana@contoso.com"}, "/users/ana%40contoso.com/drive"),
            ({"group_id": "g-1"}, "/groups/g-1/drive"),
            ({"site_id": "contoso.sharepoint.com,1a2b,3c4d"}, "/sites/contoso.sharepoint.com,1a2b,3c4d/drive"),
            ({"drive_id": "b!AbC-dE_f", "user_id": "ana@contoso.com"}, "/drives/b!AbC-dE_f"),
        ],
    )
    def test_drive_selection(self, extra, prefix):
        h = harness(response(200, file_item()))
        h.call("ONE_DRIVE_GET_ITEM", {"item_id": ITEM_ID, **extra})
        assert h.session.last.path == f"{prefix}/items/{ITEM_ID}"

    def test_move_uses_composio_camel_case(self):
        h = harness(response(200, file_item()))
        h.call("ONE_DRIVE_MOVE_ITEM", {"itemId": PERSONAL_ID, "parentReference": {"id": "D4648F06C91D9D3D!100", "driveId": "D4648F06C91D9D3D"}, "driveId": "D4648F06C91D9D3D"})
        assert h.session.last.path == f"/drives/D4648F06C91D9D3D/items/{PERSONAL_ID}"
        assert h.session.last.json == {"parentReference": {"id": "D4648F06C91D9D3D!100", "driveId": "D4648F06C91D9D3D"}}

    def test_search_scope_and_quote_escaping(self):
        h = harness(response(200, listing([])))
        h.call("ONE_DRIVE_SEARCH_ITEMS", {"q": "Ana's *.pdf plan", "search_scope": "drive", "drive_id": "b!d"})
        assert h.session.last.path == "/drives/b!d/search(q='Ana%27%27s%20.pdf%20plan')"

    def test_be_search_arguments(self):
        h = harness(response(200, listing([])))
        h.call("ONE_DRIVE_SEARCH_ITEMS", {"q": "roadmap", "top": 10})
        assert h.session.last.path == "/me/drive/root/search(q='roadmap')"
        assert h.session.last.params == {"$top": 10}

    @pytest.mark.parametrize(
        ("args", "path"),
        [
            ({}, "/me/drive/root/children"),
            ({"folder_path": "/"}, "/me/drive/root/children"),
            ({"folder_path": "Documents/Q3 #1"}, "/me/drive/root:/Documents/Q3%20%231:/children"),
            ({"folder_item_id": ITEM_ID, "folder_path": "/ignored"}, f"/me/drive/items/{ITEM_ID}/children"),
        ],
    )
    def test_children_of(self, args, path):
        h = harness(response(200, listing([])))
        h.call("ONE_DRIVE_LIST_FOLDER_CHILDREN", args)
        assert h.session.last.path == path

    def test_create_folder_in_folder_id(self):
        h = harness(response(201, folder_item("x")))
        h.call("ONE_DRIVE_ONEDRIVE_CREATE_FOLDER", {"name": "x", "parent_folder": ITEM_ID, "conflict_behavior": "fail", "description": "d"})
        assert h.session.last.path == f"/me/drive/items/{ITEM_ID}/children"
        assert h.session.last.json == {"name": "x", "folder": {}, "@microsoft.graph.conflictBehavior": "fail", "description": "d"}

    def test_update_moves_and_sends_if_match(self):
        h = harness(response(200, file_item()))
        h.call(
            "ONE_DRIVE_UPDATE_DRIVE_ITEM_METADATA",
            {"item_id": ITEM_ID, "parent_reference_id": "01P", "parent_reference_drive_id": "b!d", "if_match": "etag-1", "file_system_info": {"lastModifiedDateTime": "2026-01-01T00:00:00Z"}},
        )
        assert h.session.last.json == {"parentReference": {"id": "01P", "driveId": "b!d"}, "fileSystemInfo": {"lastModifiedDateTime": "2026-01-01T00:00:00Z"}}
        assert h.session.last.headers["If-Match"] == "etag-1"

    def test_delete_returns_the_id(self):
        h = harness(response(204))
        assert h.call("ONE_DRIVE_DELETE_ITEM", {"item_id": f" {ITEM_ID} "}).data == {"deleted": True, "item_id": ITEM_ID}

    def test_link_for_recipients(self):
        h = harness(response(200, {"id": "p"}))
        h.call(
            "ONE_DRIVE_CREATE_LINK",
            {"item_id": ITEM_ID, "type": "edit", "recipients": [{"email": "ana@example.com"}], "expiration_date_time": "2027-01-01T00:00:00Z", "retain_inherited_permissions": False},
        )
        assert h.session.last.json == {
            "type": "edit",
            "scope": "users",
            "recipients": [{"email": "ana@example.com"}],
            "expirationDateTime": "2027-01-01T00:00:00Z",
            "retainInheritedPermissions": False,
        }


class TestSecrecy:
    def test_token_only_in_the_authorization_header(self, caplog):
        caplog.set_level(logging.DEBUG)
        h = harness(graph_error(500, "generalException", "boom"))
        result = h.call("ONE_DRIVE_SEARCH_ITEMS", {"q": "salary review"})
        assert TOKEN not in str(result.to_dict())
        assert TOKEN not in caplog.text
        assert "salary" not in caplog.text  # arguments are never logged
        assert TOKEN not in repr(OneDriveToolkit().client(CONFIG))

    def test_transport_error_names_the_type_only(self):
        import requests

        h = harness(requests.ConnectionError(f"https://graph.microsoft.com/v1.0/me/drive/root:/secret-plan.docx Bearer {TOKEN}"))
        result = h.call("ONE_DRIVE_GET_ITEM_BY_PATH", {"item_path": "secret-plan.docx"})
        assert result.error == "network error talking to Microsoft Graph (ConnectionError)"

    def test_whitespace_around_the_token_is_stripped(self):
        h = harness(response(200, {"id": "d"}))
        h.call("ONE_DRIVE_GET_DRIVE", {}, config={"access_token": f"  {TOKEN}\n"})
        assert h.session.last.headers["Authorization"] == f"Bearer {TOKEN}"


def test_describe_error_without_status_is_the_message():
    assert describe_error(OneDriveAPIError(0, "network error")) == "network error"


class _CountingBody:
    """A response body that counts what is read from it (and can make each
    read take time on a fake clock, or fail)."""

    def __init__(self, size: int, *, clock=None, per_read: float = 0.0, fail_after: int | None = None):
        self.left = size
        self.read_bytes = 0
        self.reads = 0
        self.closed = False
        self.clock, self.per_read, self.fail_after = clock, per_read, fail_after

    def read(self, n: int = -1) -> bytes:
        if self.fail_after is not None and self.reads >= self.fail_after:
            import requests

            raise requests.ReadTimeout("read timed out: https://public.am.files.1drv.com/y4mSECRET")
        self.reads += 1
        if self.clock is not None:
            self.clock.now += self.per_read
        chunk = b"x" * (self.left if n < 0 else min(n, self.left))
        self.left -= len(chunk)
        self.read_bytes += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


def _streamed(body: _CountingBody):
    r = response(200, content=b"", content_type="application/octet-stream")
    r._content = False  # not read yet: .content / iter_content() pull from raw
    r._content_consumed = False
    r.raw = body
    return r


class TestStreamedDownload:
    def test_content_is_requested_as_a_stream(self):
        h = harness(response(200, file_item(item_id=ITEM_ID)), response(200, content=b"%PDF"))
        h.call("ONE_DRIVE_DOWNLOAD_FILE", {"item_id": ITEM_ID})
        assert h.session.calls[0].stream in (None, False)  # metadata
        assert h.session.calls[1].stream is True

    def test_byte_cap_stops_the_read_early(self, monkeypatch):
        monkeypatch.setattr(tools, "DOWNLOAD_MAX_BYTES", 1024 * 1024)
        body = _CountingBody(8 * 1024 * 1024)
        meta = {"id": ITEM_ID, "name": "big.bin", "file": {}}  # no size: only the stream can tell
        h = harness(response(200, meta), _streamed(body))
        result = h.call("ONE_DRIVE_DOWNLOAD_FILE", {"item_id": ITEM_ID})
        assert (result.successful, result.error_code) == (False, "invalid_arguments")
        assert "download limit" in result.error
        assert body.read_bytes <= 1024 * 1024 + 64 * 1024
        assert body.closed
        assert result.files == []

    def test_budget_is_checked_between_chunks(self):
        from aisquare_pipe_onedrive.rest import OneDriveRestClient

        from .fakes import FakeSession
        from .test_errors import FakeClock

        clock = FakeClock()
        body = _CountingBody(40 * 64 * 1024, clock=clock, per_read=1.0)  # one chunk a second for 40s
        session = FakeSession([_streamed(body)])
        client = OneDriveRestClient(TOKEN, session=session, budget=5, sleep=clock.sleep, monotonic=clock)  # type: ignore[arg-type]
        with pytest.raises(OneDriveAPIError) as exc:
            client.download_bytes(f"/me/drive/items/{ITEM_ID}/content", max_bytes=10**9)
        assert exc.value.timed_out
        assert body.reads <= 6
        assert body.closed

    def test_stalled_body_is_a_timeout(self):
        body = _CountingBody(4 * 64 * 1024, fail_after=1)
        h = harness(response(200, file_item(item_id=ITEM_ID, size=4 * 64 * 1024)), _streamed(body))
        result = h.call("ONE_DRIVE_DOWNLOAD_FILE", {"item_id": ITEM_ID})
        assert result.error_code == "timeout"
        assert "1drv.com" not in result.error and "SECRET" not in result.error
        assert body.closed
