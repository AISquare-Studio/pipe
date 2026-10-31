"""GoogleDriveToolkit behaviour beyond the per-tool CASES: catalog identity,
argument aliases and forgiveness, query building, downloads and exports,
write guards, secrecy and tool selection."""

from __future__ import annotations

import base64
import logging

import pytest
from aisquare.pipe import AuthType, get_toolkit

from aisquare_pipe_googledrive import GoogleDriveToolkit
from aisquare_pipe_googledrive.formats import DOCUMENT, PRESENTATION, SPREADSHEET, XLSX
from aisquare_pipe_googledrive.rest import (
    FOLDER_MIME,
    SHORTCUT_MIME,
    GoogleDriveAPIError,
)
from aisquare_pipe_googledrive.toolkit import describe_error, failure
from aisquare_pipe_googledrive.tools import (
    INLINE_TEXT_MAX,
    apply_aliases,
    drive_id,
    search_query,
)

from .fakes import CONFIG, TOKEN, drive_error, file_meta, harness, response

COMPOSIO_SLUGS = {
    "GOOGLEDRIVE_FIND_FILE",
    "GOOGLEDRIVE_GET_FILE_METADATA",
    "GOOGLEDRIVE_DOWNLOAD_FILE",
    "GOOGLEDRIVE_LIST_FILES",
    "GOOGLEDRIVE_FIND_FOLDER",
    "GOOGLEDRIVE_CREATE_FOLDER",
    "GOOGLEDRIVE_UPLOAD_FILE",
    "GOOGLEDRIVE_CREATE_FILE_FROM_TEXT",
    "GOOGLEDRIVE_EDIT_FILE",
    "GOOGLEDRIVE_COPY_FILE",
    "GOOGLEDRIVE_MOVE_FILE",
    "GOOGLEDRIVE_UPDATE_FILE_PUT",
    "GOOGLEDRIVE_DELETE_FILE",
    "GOOGLEDRIVE_TRASH_FILE",
    "GOOGLEDRIVE_UNTRASH_FILE",
    "GOOGLEDRIVE_ADD_FILE_SHARING_PREFERENCE",
    "GOOGLEDRIVE_CREATE_PERMISSION",
    "GOOGLEDRIVE_LIST_PERMISSIONS",
    "GOOGLEDRIVE_EXPORT_GOOGLE_WORKSPACE_FILE",
    "GOOGLEDRIVE_GET_ABOUT",
    "GOOGLEDRIVE_LIST_SHARED_DRIVES",
    "GOOGLEDRIVE_LIST_COMMENTS",
    "GOOGLEDRIVE_CREATE_COMMENT",
}


def _tool(slug: str):
    tool = GoogleDriveToolkit().get_tool(slug)
    assert tool is not None
    return tool


class TestCatalog:
    def test_registered_as_the_googledrive_toolkit(self):
        assert get_toolkit("googledrive") is GoogleDriveToolkit

    def test_identity(self):
        toolkit = GoogleDriveToolkit()
        assert (toolkit.name, toolkit.slug_prefix, toolkit.version) == ("googledrive", "GOOGLEDRIVE_", "0.3.0")
        assert toolkit.auth_type is AuthType.OAUTH2

    def test_composio_slugs_exactly(self):
        assert {t.slug for t in GoogleDriveToolkit().tools()} == COMPOSIO_SLUGS

    def test_composio_argument_casing(self):
        assert _tool("GOOGLEDRIVE_DOWNLOAD_FILE").input_schema["required"] == ["fileId"]
        assert set(_tool("GOOGLEDRIVE_DOWNLOAD_FILE").input_schema["properties"]) == {"fileId", "mime_type"}
        assert _tool("GOOGLEDRIVE_GET_FILE_METADATA").input_schema["required"] == ["fileId"]
        assert _tool("GOOGLEDRIVE_EXPORT_GOOGLE_WORKSPACE_FILE").input_schema["required"] == ["fileId", "mimeType"]
        assert _tool("GOOGLEDRIVE_MOVE_FILE").input_schema["required"] == ["file_id"]
        assert "folder_id" in _tool("GOOGLEDRIVE_FIND_FILE").input_schema["properties"]
        assert "folderId" in _tool("GOOGLEDRIVE_LIST_FILES").input_schema["properties"]
        assert "page_token" in _tool("GOOGLEDRIVE_FIND_FOLDER").input_schema["properties"]

    def test_read_only_and_destructive_flags(self):
        tools = {t.slug: t for t in GoogleDriveToolkit().tools()}
        destructive = {s for s, t in tools.items() if t.destructive}
        assert destructive == {"GOOGLEDRIVE_DELETE_FILE", "GOOGLEDRIVE_TRASH_FILE", "GOOGLEDRIVE_EDIT_FILE"}
        read_only = {s for s, t in tools.items() if t.read_only}
        assert read_only == {
            "GOOGLEDRIVE_FIND_FILE",
            "GOOGLEDRIVE_LIST_FILES",
            "GOOGLEDRIVE_FIND_FOLDER",
            "GOOGLEDRIVE_GET_FILE_METADATA",
            "GOOGLEDRIVE_DOWNLOAD_FILE",
            "GOOGLEDRIVE_EXPORT_GOOGLE_WORKSPACE_FILE",
            "GOOGLEDRIVE_GET_ABOUT",
            "GOOGLEDRIVE_LIST_SHARED_DRIVES",
            "GOOGLEDRIVE_LIST_PERMISSIONS",
            "GOOGLEDRIVE_LIST_COMMENTS",
        }

    @pytest.mark.parametrize(
        ("config", "ok"),
        [({}, False), ({"access_token": ""}, False), ({"access_token": "a b"}, False), ({"access_token": " ya29.x\n"}, True)],
    )
    def test_validate_config(self, config, ok):
        assert GoogleDriveToolkit().validate_config(config) is ok

    def test_no_credentials_means_no_request(self):
        h = harness()
        result = h.call("GOOGLEDRIVE_FIND_FILE", {"q": "x"}, config={"access_token": " "})
        assert result.error_code == "not_connected"
        assert h.session.calls == []


class TestAliases:
    def test_snake_case_file_id_reaches_a_camel_case_schema(self):
        h = harness(response(200, file_meta()))
        assert h.call("GOOGLEDRIVE_GET_FILE_METADATA", {"file_id": "file123"}).successful
        assert h.session.last.path == "/files/file123"

    def test_camel_case_file_id_reaches_a_snake_case_schema(self):
        h = harness(response(200, file_meta()))
        assert h.call("GOOGLEDRIVE_COPY_FILE", {"fileId": "file123"}).successful
        assert h.session.last.path == "/files/file123/copy"

    def test_the_schema_spelling_wins_over_an_alias(self):
        tool = _tool("GOOGLEDRIVE_GET_FILE_METADATA")
        assert apply_aliases(tool, {"fileId": "a", "file_id": "b"}) == {"fileId": "a", "file_id": "b"}

    def test_create_file_from_text_aliases(self):
        h = harness(response(200, file_meta("n1", "t")))
        result = h.call("GOOGLEDRIVE_CREATE_FILE_FROM_TEXT", {"title": "t", "content": "hello", "folder_id": "fold1234567890"})
        assert result.successful, result.error
        assert b'{"name": "t", "parents": ["fold1234567890"]}' in h.session.last.data

    def test_lowercase_slug_still_gets_aliases(self):
        h = harness(response(200, file_meta()))
        assert h.call("googledrive_get_file_metadata", {"file_id": "file123"}).successful

    def test_non_mapping_arguments_fail_validation_not_the_call(self):
        result = harness().call("GOOGLEDRIVE_GET_FILE_METADATA", ["file123"])  # type: ignore[arg-type]
        assert result.error_code == "invalid_arguments"


class TestArguments:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("1AbCdEfGhIjKlMnOp", "1AbCdEfGhIjKlMnOp"),
            ("  1AbCdEfGhIjKlMnOp \n", "1AbCdEfGhIjKlMnOp"),
            ("https://docs.google.com/document/d/1AbCdEfGhIjKlMnOp/edit?usp=sharing", "1AbCdEfGhIjKlMnOp"),
            ("https://drive.google.com/file/d/1AbCdEfGhIjKlMnOp/view", "1AbCdEfGhIjKlMnOp"),
            ("https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOp", "1AbCdEfGhIjKlMnOp"),
            ("https://drive.google.com/open?id=1AbCdEfGhIjKlMnOp", "1AbCdEfGhIjKlMnOp"),
            ("https://example.com/nothing-here", "https://example.com/nothing-here"),
        ],
    )
    def test_drive_id_from_ids_and_urls(self, value, expected):
        assert drive_id(value) == expected

    def test_a_docs_url_works_as_file_id(self):
        h = harness(response(200, file_meta("1AbCdEfGhIjKlMnOp")))
        h.call("GOOGLEDRIVE_GET_FILE_METADATA", {"fileId": "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOp/edit"})
        assert h.session.last.path == "/files/1AbCdEfGhIjKlMnOp"

    def test_ids_cannot_inject_url_syntax(self):
        h = harness(response(200, file_meta()))
        h.call("GOOGLEDRIVE_GET_FILE_METADATA", {"fileId": "abc/../../about?x=1"})
        assert h.session.last.url.endswith("/files/abc%2F..%2F..%2Fabout%3Fx%3D1")

    @pytest.mark.parametrize(
        ("q", "expected"),
        [
            ("roadmap", "fullText contains 'roadmap' and trashed = false"),
            ("Q3 plan's draft", "fullText contains 'Q3 plan\\'s draft' and trashed = false"),
            ("ada@example.com", "'ada@example.com' in owners and trashed = false"),
            ("name contains 'roadmap'", "name contains 'roadmap'"),
            ("mimeType = 'application/pdf'", "mimeType = 'application/pdf'"),
            ("'abc' in parents", "'abc' in parents"),
            ("modifiedTime > '2026-01-01'", "modifiedTime > '2026-01-01'"),
            ("", ""),
        ],
    )
    def test_search_query(self, q, expected):
        assert search_query(q) == expected

    def test_folder_id_is_anded_onto_the_query(self):
        h = harness(response(200, {"files": []}))
        h.call("GOOGLEDRIVE_FIND_FILE", {"q": "name contains 'a' or name contains 'b'", "folder_id": "fold1234567890"})
        assert h.session.last.params["q"] == "(name contains 'a' or name contains 'b') and 'fold1234567890' in parents"

    def test_no_query_hides_the_trash(self):
        h = harness(response(200, {"files": []}))
        h.call("GOOGLEDRIVE_FIND_FILE", {})
        assert h.session.last.params["q"] == "trashed = false"

    def test_drive_id_searches_one_shared_drive(self):
        h = harness(response(200, {"files": []}))
        h.call("GOOGLEDRIVE_FIND_FILE", {"q": "x = 'y'", "driveId": "0AbcDrive"})
        params = h.session.last.params
        assert (params["corpora"], params["driveId"], params["includeItemsFromAllDrives"], params["supportsAllDrives"]) == (
            "drive",
            "0AbcDrive",
            "true",
            "true",
        )

    def test_user_corpora_can_leave_shared_drives_out(self):
        h = harness(response(200, {"files": []}))
        h.call("GOOGLEDRIVE_FIND_FILE", {"corpora": "user", "includeItemsFromAllDrives": False, "supportsAllDrives": False})
        params = h.session.last.params
        assert (params["corpora"], params["includeItemsFromAllDrives"], params["supportsAllDrives"]) == ("user", "false", "false")

    @pytest.mark.parametrize(
        ("fields", "expected"),
        [
            ("id,name", "nextPageToken,incompleteSearch,files(id,name)"),
            ("files(id)", "nextPageToken,files(id)"),
            ("nextPageToken,files(id)", "nextPageToken,files(id)"),
            ("*", "*"),
        ],
    )
    def test_list_fields_always_carry_the_page_token(self, fields, expected):
        h = harness(response(200, {"files": []}))
        h.call("GOOGLEDRIVE_FIND_FILE", {"fields": fields})
        assert h.session.last.params["fields"] == expected

    def test_page_size_is_clamped(self):
        h = harness(response(200, {"files": []}))
        h.call("GOOGLEDRIVE_LIST_FILES", {"pageSize": 5000})
        assert h.session.last.params["pageSize"] == 1000

    def test_find_folder_escapes_names(self):
        h = harness(response(200, {"files": []}))
        h.call("GOOGLEDRIVE_FIND_FOLDER", {"name_exact": "Ada's 'stuff'", "starred": False})
        q = h.session.last.params["q"]
        assert "name = 'Ada\\'s \\'stuff\\''" in q
        assert q.endswith("starred = false")


class TestDownload:
    def test_text_file_is_inlined_and_returned_as_a_file(self):
        h = harness(response(200, file_meta(modifiedTime="2026-10-01T00:00:00Z", webViewLink="https://drive/x")), response(200, content=b"hello"))
        result = h.call("GOOGLEDRIVE_DOWNLOAD_FILE", {"fileId": "file123"})
        assert result.data == {
            "id": "file123",
            "name": "notes.txt",
            "mimetype": "text/plain",
            "size": 5,
            "exported": False,
            "modifiedTime": "2026-10-01T00:00:00Z",
            "webViewLink": "https://drive/x",
            "content": "hello",
        }
        (envelope,) = result.files
        assert (envelope.content_type, envelope.data) == ("text/plain", b"hello")
        assert envelope.metadata["filename"] == "notes.txt"
        assert envelope.metadata["file_id"] == "file123"
        assert envelope.source_id == "googledrive:file123"

    def test_binary_file_is_not_inlined(self):
        h = harness(response(200, file_meta(name="a.pdf", mime="application/pdf")), response(200, content=b"%PDF-1.7\xff"))
        result = h.call("GOOGLEDRIVE_DOWNLOAD_FILE", {"fileId": "file123"})
        assert "content" not in result.data
        assert result.files[0].data == b"%PDF-1.7\xff"

    def test_large_text_is_not_inlined(self):
        body = b"a" * (INLINE_TEXT_MAX + 1)
        h = harness(response(200, file_meta(size=len(body))), response(200, content=body))
        result = h.call("GOOGLEDRIVE_DOWNLOAD_FILE", {"fileId": "file123"})
        assert "content" not in result.data
        assert result.data["size"] == len(body)

    @pytest.mark.parametrize(
        ("mime", "export", "name"),
        [
            (DOCUMENT, "application/pdf", "Roadmap.pdf"),
            (PRESENTATION, "application/pdf", "Roadmap.pdf"),
            (SPREADSHEET, XLSX, "Roadmap.xlsx"),
        ],
    )
    def test_native_files_are_exported_by_default(self, mime, export, name):
        h = harness(response(200, file_meta("doc1", "Roadmap", mime, None)), response(200, content=b"bytes"))
        result = h.call("GOOGLEDRIVE_DOWNLOAD_FILE", {"fileId": "doc1"})
        assert h.session.last.path == "/files/doc1/export"
        assert h.session.last.params == {"mimeType": export}
        assert (result.data["name"], result.data["mimetype"], result.data["exported"]) == (name, export, True)
        assert result.data["original_mimetype"] == mime
        assert result.files[0].content_type == export

    def test_mime_type_picks_the_export_format(self):
        h = harness(response(200, file_meta("doc1", "Roadmap", DOCUMENT, None)), response(200, content=b"# Roadmap"))
        result = h.call("GOOGLEDRIVE_DOWNLOAD_FILE", {"fileId": "doc1", "mime_type": "text/markdown"})
        assert h.session.last.params == {"mimeType": "text/markdown"}
        assert (result.data["name"], result.data["content"]) == ("Roadmap.md", "# Roadmap")

    def test_mime_type_is_ignored_for_stored_files(self):
        h = harness(response(200, file_meta(name="a.pdf", mime="application/pdf")), response(200, content=b"%PDF"))
        h.call("GOOGLEDRIVE_DOWNLOAD_FILE", {"fileId": "file123", "mime_type": "text/plain"})
        assert h.session.last.params == {"alt": "media", "supportsAllDrives": "true"}

    def test_shortcut_is_followed_once(self):
        shortcut = file_meta("short1", "Link", SHORTCUT_MIME, None, shortcutDetails={"targetId": "target123"})
        h = harness(response(200, shortcut), response(200, file_meta("target123", "real.txt")), response(200, content=b"x"))
        result = h.call("GOOGLEDRIVE_DOWNLOAD_FILE", {"fileId": "short1"})
        assert [c.path for c in h.session.calls] == ["/files/short1", "/files/target123", "/files/target123"]
        assert result.data["name"] == "real.txt"

    def test_already_suffixed_names_keep_their_extension(self):
        h = harness(response(200, file_meta("doc1", "Report.pdf", DOCUMENT, None)), response(200, content=b"%PDF"))
        assert h.call("GOOGLEDRIVE_DOWNLOAD_FILE", {"fileId": "doc1"}).data["name"] == "Report.pdf"

    def test_export_tool(self):
        h = harness(response(200, file_meta("sheet1", "Budget", SPREADSHEET, None)), response(200, content=b"a,b\n1,2\n"))
        result = h.call("GOOGLEDRIVE_EXPORT_GOOGLE_WORKSPACE_FILE", {"fileId": "sheet1", "mimeType": "text/csv"})
        assert (result.data["name"], result.data["content"]) == ("Budget.csv", "a,b\n1,2\n")


class TestWrites:
    def test_move_drops_the_current_parent(self):
        h = harness(response(200, {"parents": ["old1234567"]}), response(200, file_meta(parents=["new1234567"])))
        result = h.call("GOOGLEDRIVE_MOVE_FILE", {"file_id": "file123", "add_parents": "new1234567"})
        assert result.successful, result.error
        lookup, patch = h.session.calls
        assert lookup.params == {"fields": "parents", "supportsAllDrives": "true"}
        assert (patch.params["addParents"], patch.params["removeParents"]) == ("new1234567", "old1234567")

    def test_move_into_the_current_folder_removes_nothing(self):
        h = harness(response(200, {"parents": ["same123456"]}), response(200, file_meta()))
        h.call("GOOGLEDRIVE_MOVE_FILE", {"file_id": "file123", "add_parents": "same123456"})
        assert "removeParents" not in h.session.last.params

    def test_create_folder_in_a_parent_given_by_id(self):
        h = harness(response(200, {"id": "par1234567", "mimeType": FOLDER_MIME}), response(200, {"id": "new"}))
        h.call("GOOGLEDRIVE_CREATE_FOLDER", {"name": "Q3", "parent_id": "par1234567"})
        assert h.session.last.json == {"name": "Q3", "mimeType": FOLDER_MIME, "parents": ["par1234567"]}

    def test_create_folder_in_a_parent_given_by_name(self):
        h = harness(
            drive_error(404, "File not found: Reports 2026.", "notFound"),
            response(200, {"files": [{"id": "rep1234567", "name": "Reports 2026"}]}),
            response(200, {"id": "new"}),
        )
        result = h.call("GOOGLEDRIVE_CREATE_FOLDER", {"name": "Q3", "parent_id": "Reports 2026"})
        assert result.successful, result.error
        search = h.session.calls[1]
        assert search.params["q"] == f"mimeType = '{FOLDER_MIME}' and trashed = false and name = 'Reports 2026'"
        assert h.session.last.json["parents"] == ["rep1234567"]

    def test_create_folder_parent_lookup_failure_other_than_404_is_reported(self):
        h = harness(drive_error(403, "Forbidden", "insufficientPermissions"))
        result = h.call("GOOGLEDRIVE_CREATE_FOLDER", {"name": "Q3", "parent_id": "par1234567"})
        assert result.error_code == "forbidden"

    def test_text_becomes_a_google_doc(self):
        h = harness(response(200, {"id": "d1", "mimeType": DOCUMENT}))
        h.call("GOOGLEDRIVE_CREATE_FILE_FROM_TEXT", {"file_name": "Notes", "text_content": "hi", "mime_type": DOCUMENT})
        body = h.session.last.data
        assert f'{{"name": "Notes", "mimeType": "{DOCUMENT}"}}'.encode() in body
        assert b"Content-Type: text/plain\r\n\r\nhi" in body

    def test_csv_becomes_a_google_sheet(self):
        h = harness(response(200, {"id": "s1"}))
        h.call("GOOGLEDRIVE_CREATE_FILE_FROM_TEXT", {"file_name": "B", "text_content": "a,b", "mime_type": SPREADSHEET})
        assert b"Content-Type: text/csv\r\n\r\na,b" in h.session.last.data

    def test_upload_decodes_base64_and_guesses_the_type(self):
        h = harness(response(200, {"id": "u1"}))
        payload = base64.b64encode(b"%PDF-1.7").decode()
        h.call("GOOGLEDRIVE_UPLOAD_FILE", {"file_to_upload": {"name": "r.pdf", "file_content_bytes": payload}})
        body = h.session.last.data
        assert b'{"name": "r.pdf"}' in body
        assert b"Content-Type: application/pdf\r\n\r\n%PDF-1.7\r\n" in body

    def test_update_maps_viewers_can_copy_content(self):
        h = harness(response(200, file_meta()))
        h.call("GOOGLEDRIVE_UPDATE_FILE_PUT", {"fileId": "file123", "viewers_can_copy_content": False, "description": "d"})
        assert h.session.last.json == {"description": "d", "copyRequiresWriterPermission": True}

    def test_owner_transfer(self):
        h = harness(response(200, {"id": "p"}))
        h.call(
            "GOOGLEDRIVE_ADD_FILE_SHARING_PREFERENCE",
            {"file_id": "f1", "role": "owner", "type": "user", "email_address": "a@b.co", "transfer_ownership": True},
        )
        assert h.session.last.params["transferOwnership"] == "true"

    def test_comment_with_quote(self):
        h = harness(response(200, {"id": "c"}))
        h.call(
            "GOOGLEDRIVE_CREATE_COMMENT",
            {"file_id": "f1", "content": "typo", "quoted_file_content_value": "teh", "quoted_file_content_mime_type": "text/plain"},
        )
        assert h.session.last.json == {"content": "typo", "quotedFileContent": {"value": "teh", "mimeType": "text/plain"}}


class TestSecrecy:
    def test_token_echoed_in_an_error_is_scrubbed(self):
        h = harness(drive_error(400, f"bad token {TOKEN}", "invalid"))
        result = h.call("GOOGLEDRIVE_GET_ABOUT", {})
        assert TOKEN not in result.error
        assert "***" in result.error

    def test_network_error_carries_no_url_or_arguments(self):
        import requests

        h = harness(requests.ConnectionError(f"https://www.googleapis.com/drive/v3/files?q=secret&access_token={TOKEN}"))
        result = h.call("GOOGLEDRIVE_FIND_FILE", {"q": "secret"})
        assert result.error == "network error talking to Google Drive (ConnectionError)"

    def test_logs_carry_no_token_or_arguments(self, caplog):
        h = harness(drive_error(503, "Backend Error", "backendError"), response(200, {"files": []}))
        with caplog.at_level(logging.DEBUG):
            h.call("GOOGLEDRIVE_FIND_FILE", {"q": "name contains 'secret-plan'"})
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert TOKEN not in text and "secret-plan" not in text

    def test_config_is_not_mutated(self):
        config = dict(CONFIG)
        harness(response(200, {})).call("GOOGLEDRIVE_GET_ABOUT", {}, config=config)
        assert config == CONFIG


class TestDescribeError:
    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (GoogleDriveAPIError(0, "network error talking to Google Drive (ConnectionError)"), "network error talking to Google Drive (ConnectionError)"),
            (GoogleDriveAPIError(401, "Invalid Credentials"), "must be reconnected"),
            (GoogleDriveAPIError(403, "Rate", retry_after=2.2), "retry in about 3s"),
            (GoogleDriveAPIError(403, "Insufficient Permission"), "not allowed to do that"),
            (GoogleDriveAPIError(404, "File not found"), "cannot see it"),
            (GoogleDriveAPIError(500, "Internal Error"), "500: Internal Error"),
        ],
    )
    def test_messages(self, error, expected):
        assert expected in describe_error(error)


class TestToolSelection:
    @pytest.mark.parametrize(
        ("query", "slug"),
        [
            ("find the roadmap document", "GOOGLEDRIVE_FIND_FILE"),
            ("download the budget file", "GOOGLEDRIVE_DOWNLOAD_FILE"),
            ("export the doc as markdown", "GOOGLEDRIVE_EXPORT_GOOGLE_WORKSPACE_FILE"),
            ("create a new folder", "GOOGLEDRIVE_CREATE_FOLDER"),
            ("share the file with a colleague", "GOOGLEDRIVE_ADD_FILE_SHARING_PREFERENCE"),
            ("how much storage space do I have", "GOOGLEDRIVE_GET_ABOUT"),
            ("rename the file", "GOOGLEDRIVE_UPDATE_FILE_PUT"),
            ("list comments on the doc", "GOOGLEDRIVE_LIST_COMMENTS"),
            ("restore from trash", "GOOGLEDRIVE_UNTRASH_FILE"),
        ],
    )
    def test_in_top_three(self, query, slug):
        assert slug in [t.slug for t in GoogleDriveToolkit().search_tools(query, limit=3)]


class TestDownloadCap:
    def test_export_larger_than_the_cap_is_refused(self, monkeypatch):
        monkeypatch.setattr("aisquare_pipe_googledrive.tools.DOWNLOAD_MAX_BYTES", 10)
        h = harness(response(200, file_meta("doc1", "Plan", DOCUMENT, None)), response(200, content=b"x" * 11))
        result = h.call("GOOGLEDRIVE_DOWNLOAD_FILE", {"fileId": "doc1"})
        assert result.error_code == "invalid_arguments"
        assert "download limit" in result.error

    def test_metadata_size_over_the_cap_is_refused_before_downloading(self, monkeypatch):
        monkeypatch.setattr("aisquare_pipe_googledrive.tools.DOWNLOAD_MAX_BYTES", 10)
        h = harness(response(200, file_meta(size=11)))
        result = h.call("GOOGLEDRIVE_DOWNLOAD_FILE", {"fileId": "file123"})
        assert result.error_code == "invalid_arguments"
        assert len(h.session.calls) == 1


class TestQueryIsolation:
    """A caller's q is ANDed with the folder scope as one group, and a q that
    could break out of its group is refused before any request."""

    @pytest.mark.parametrize(
        ("slug", "folder_arg"), [("GOOGLEDRIVE_FIND_FILE", "folder_id"), ("GOOGLEDRIVE_LIST_FILES", "folderId")]
    )
    @pytest.mark.parametrize("sep", [" ", "\n", "\t"])
    def test_or_query_stays_inside_the_folder(self, slug, folder_arg, sep):
        q = f"name contains 'a'{sep}or{sep}name contains 'b'"
        h = harness(response(200, {"files": []}))
        h.call(slug, {"q": q, folder_arg: "FOLDER_A1234"})
        assert h.session.last.params["q"] == f"({q}) and 'FOLDER_A1234' in parents"

    @pytest.mark.parametrize(
        "q",
        [
            "name contains 'a') or (name contains 'b'",
            "name contains 'a'))",
            "((name contains 'a'",
            "name contains 'unterminated",
            "name = 'it\\'s",
        ],
    )
    def test_query_that_escapes_its_group_is_refused(self, q):
        h = harness()
        result = h.call("GOOGLEDRIVE_FIND_FILE", {"q": q, "folder_id": "FOLDER_A1234"})
        assert result.error_code == "invalid_arguments"
        assert "q" in result.error
        assert h.session.calls == []

    def test_parentheses_inside_quotes_do_not_count(self):
        h = harness(response(200, {"files": []}))
        h.call("GOOGLEDRIVE_FIND_FILE", {"q": "name contains ')(' and name contains 'it\\'s ('", "folder_id": "F1234567890"})
        assert h.session.last.params["q"] == "(name contains ')(' and name contains 'it\\'s (') and 'F1234567890' in parents"

    def test_query_alone_is_sent_verbatim(self):
        h = harness(response(200, {"files": []}))
        h.call("GOOGLEDRIVE_FIND_FILE", {"q": "name contains 'a' or name contains 'b'"})
        assert h.session.last.params["q"] == "name contains 'a' or name contains 'b'"

    @pytest.mark.parametrize(
        ("q", "expected"),
        [
            ("what has changed", "fullText contains 'what has changed' and trashed = false"),
            ("contains budget", "fullText contains 'contains budget' and trashed = false"),
            ("files in parents folder", "fullText contains 'files in parents folder' and trashed = false"),
            ("name contains 'x'", "name contains 'x'"),
            ("properties has { key='a' and value='b' }", "properties has { key='a' and value='b' }"),
            ("'abc' in parents", "'abc' in parents"),
            ("trashed=false", "trashed=false"),
        ],
    )
    def test_only_operator_forms_count_as_a_query(self, q, expected):
        assert search_query(q) == expected


class TestWholeSeconds:
    def test_non_finite_retry_after_is_reported_without_a_number(self):
        error = GoogleDriveAPIError(429, "slow", retry_after=float("inf"))
        assert describe_error(error).endswith("Google Drive rate limit; retry later")
        result = failure(error)
        assert (result.error_code, result.retry_after) == ("rate_limited", None)

    def test_finite_retry_after_rounds_up(self):
        assert failure(GoogleDriveAPIError(429, "slow", retry_after=2.1)).retry_after == 3.0
