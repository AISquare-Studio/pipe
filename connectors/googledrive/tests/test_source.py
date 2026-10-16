"""GoogleDriveSource: folder walks, exports of Google-native files, filters,
streaming, pagination and Drive failures — HTTP mocked at the client boundary."""

from __future__ import annotations

import pytest
from aisquare.pipe import ConfigValidationError, PipelineError, PullParams

from aisquare_pipe_googledrive import GoogleDriveSource
from aisquare_pipe_googledrive.formats import DOCX, PRESENTATION, SPREADSHEET, XLSX
from aisquare_pipe_googledrive.rest import FOLDER_MIME, SHORTCUT_MIME

from .fakes import CONFIG, FakeSession, drive_error, file_meta, rate_limited, response

DOC = "application/vnd.google-apps.document"
FORM = "application/vnd.google-apps.form"
ITEM_FIELDS = "nextPageToken,files(id,name,mimeType,size,modifiedTime,webViewLink,md5Checksum,driveId)"
ALL_DRIVES = {"supportsAllDrives": "true", "includeItemsFromAllDrives": "true", "corpora": "allDrives"}


def source(*answers, **kwargs):
    session = FakeSession(list(answers))
    sleeps: list[float] = []
    return GoogleDriveSource(session=session, sleep=sleeps.append, **kwargs), session, sleeps  # type: ignore[arg-type]


def files(*items, token=None):
    body = {"files": list(items)}
    if token:
        body["nextPageToken"] = token
    return response(200, body)


def files_q(folder: str, *extra: str) -> str:
    return " and ".join([f"'{folder}' in parents and trashed = false", f"mimeType != '{FOLDER_MIME}'", *extra])


def folders_q(folder: str) -> str:
    return f"'{folder}' in parents and trashed = false and mimeType = '{FOLDER_MIME}'"


def pull(src, **params):
    return list(src.pull(CONFIG, PullParams(params)))


class TestPull:
    def test_files_in_root_with_native_exports(self):
        listing = files(
            file_meta("pdf1", "a.pdf", "application/pdf", 4, modifiedTime="2026-10-01T00:00:00Z", webViewLink="https://d/pdf1", md5Checksum="abc"),
            file_meta("doc1", "Plan", DOC, None),
            file_meta("sheet1", "Budget", SPREADSHEET, None),
            file_meta("slides1", "Deck", PRESENTATION, None),
            file_meta("form1", "Survey", FORM, None),
            file_meta("short1", "Link", SHORTCUT_MIME, None),
        )
        src, session, _ = source(
            listing,
            response(200, content=b"%PDF"),
            response(200, content=b"docx-bytes"),
            response(200, content=b"a,b\n"),
            response(200, content=b"%PDF deck"),
        )
        envelopes = pull(src)
        listing_call = session.calls[0]
        assert listing_call.path == "/files"
        assert listing_call.params == {"q": files_q("root"), "fields": ITEM_FIELDS, "pageSize": 1000, **ALL_DRIVES, "orderBy": "name"}
        assert [(c.path, c.params) for c in session.calls[1:]] == [
            ("/files/pdf1", {"alt": "media", "supportsAllDrives": "true"}),
            ("/files/doc1/export", {"mimeType": DOCX}),
            ("/files/sheet1/export", {"mimeType": "text/csv"}),
            ("/files/slides1/export", {"mimeType": "application/pdf"}),
        ]
        assert [(e.metadata["filename"], e.content_type, e.data) for e in envelopes] == [
            ("a.pdf", "application/pdf", b"%PDF"),
            ("Plan.docx", DOCX, b"docx-bytes"),
            ("Budget.csv", "text/csv", b"a,b\n"),
            ("Deck.pdf", "application/pdf", b"%PDF deck"),
        ]
        assert envelopes[0].metadata == {
            "filename": "a.pdf",
            "file_id": "pdf1",
            "mime_type": "application/pdf",
            "path": "a.pdf",
            "name": "a.pdf",
            "source_mime_type": "application/pdf",
            "exported": False,
            "modified_time": "2026-10-01T00:00:00Z",
            "web_view_link": "https://d/pdf1",
            "md5_checksum": "abc",
            "size": 4,
        }
        assert envelopes[1].metadata["exported"] is True
        assert envelopes[1].metadata["source_mime_type"] == DOC
        assert all(e.source_id == "googledrive-source" for e in envelopes)

    def test_listing_follows_page_tokens(self):
        src, session, _ = source(
            files(file_meta("f1", "a.txt"), token="tok2"),
            response(200, content=b"a"),
            files(file_meta("f2", "b.txt")),
            response(200, content=b"b"),
        )
        assert [e.data for e in pull(src)] == [b"a", b"b"]
        assert session.calls[2].params["pageToken"] == "tok2"

    def test_recursive_walk_builds_paths_and_visits_each_folder_once(self):
        src, session, _ = source(
            files(file_meta("f1", "top.txt")),
            response(200, content=b"top"),
            files({"id": "sub1", "name": "Sub"}, {"id": "sub2", "name": "Other"}),
            files(file_meta("f2", "inner.txt")),
            response(200, content=b"inner"),
            files({"id": "sub1", "name": "Sub again"}),  # a folder reachable twice
            files(),
            files(),
        )
        envelopes = pull(src, recursive=True)
        assert [e.metadata["path"] for e in envelopes] == ["top.txt", "Sub/inner.txt"]
        listing_qs = [c.params["q"] for c in session.calls if c.path == "/files" and c.params]
        assert listing_qs == [
            files_q("root"),
            folders_q("root"),
            files_q("sub1"),
            folders_q("sub1"),
            files_q("sub2"),
            folders_q("sub2"),
        ]

    def test_path_is_resolved_folder_by_folder(self):
        src, session, _ = source(
            files({"id": "rep1", "name": "Reports"}),
            files({"id": "y2026", "name": "2026"}),
            files(),
        )
        assert pull(src, path="/Reports/2026/") == []
        lookups = [c.params["q"] for c in session.calls[:2]]
        assert lookups == [
            f"'root' in parents and name = 'Reports' and mimeType = '{FOLDER_MIME}' and trashed = false",
            f"'rep1' in parents and name = '2026' and mimeType = '{FOLDER_MIME}' and trashed = false",
        ]
        assert session.calls[0].params["orderBy"] == "createdTime"
        assert session.last.params["q"] == files_q("y2026")

    def test_missing_path_is_a_pipeline_error(self):
        src, _, _ = source(files())
        with pytest.raises(PipelineError, match="not found"):
            pull(src, path="Nope")

    def test_dot_segments_are_refused(self):
        src, session, _ = source()
        with pytest.raises(ValueError):
            pull(src, path="Reports/../Secrets")
        assert session.calls == []

    @pytest.mark.parametrize(("params", "folder"), [({"folder_id": "fold1"}, "fold1"), ({"drive_id": "0ADrive"}, "0ADrive")])
    def test_folder_and_shared_drive_roots(self, params, folder):
        src, session, _ = source(files())
        pull(src, **params)
        assert session.last.params["q"] == files_q(folder)

    def test_modified_after_and_query_narrow_the_listing(self):
        src, session, _ = source(files())
        pull(src, modified_after="2026-01-01T00:00:00", query="starred = true")
        assert session.last.params["q"] == files_q("root", "modifiedTime > '2026-01-01T00:00:00'", "(starred = true)")

    def test_mime_type_extension_and_limit_filters(self):
        listing = files(
            file_meta("a", "a.png", "image/png", 1),
            file_meta("b", "b.txt", "text/plain", 1),
            file_meta("c", "Notes", DOC, None),
            file_meta("d", "d.jpg", "image/jpeg", 1),
        )
        src, session, _ = source(listing, response(200, content=b"png"))
        envelopes = pull(src, mime_types=["image/*", DOC], extensions=[".png", ".docx"], limit=1)
        assert [e.metadata["filename"] for e in envelopes] == ["a.png"]
        assert len(session.calls) == 2

    def test_mime_filter_matches_the_exported_type_too(self):
        src, _, _ = source(files(file_meta("s", "Budget", SPREADSHEET, None)), response(200, content=b"x"))
        assert [e.metadata["filename"] for e in pull(src, mime_types=["text/csv"])] == ["Budget.csv"]

    def test_export_formats_override_the_defaults(self):
        src, session, _ = source(files(file_meta("s", "Budget", SPREADSHEET, None)), response(200, content=b"x"))
        (envelope,) = pull(src, export_formats={SPREADSHEET: XLSX})
        assert session.last.params == {"mimeType": XLSX}
        assert envelope.metadata["filename"] == "Budget.xlsx"

    def test_large_files_are_streamed(self):
        src, session, _ = source(files(file_meta("big", "big.bin", "application/octet-stream", 10)), response(200, content=b"0123456789"))
        (envelope,) = pull(src, stream_threshold=5)
        assert session.last.stream is True
        assert envelope.data == b""
        assert envelope.stream is not None and envelope.stream.read() == b"0123456789"
        assert envelope.metadata["size"] == 10
        assert src.supports_streaming() is True


class TestPullFailures:
    def test_no_credentials(self):
        src, session, _ = source()
        with pytest.raises(ConfigValidationError):
            next(iter(src.pull({})))
        assert session.calls == []

    def test_rejected_token_says_reconnect(self):
        src, _, _ = source(drive_error(401, "Invalid Credentials", "authError"))
        with pytest.raises(ConfigValidationError, match="must be reconnected"):
            pull(src)

    def test_rate_limit_is_retried(self):
        src, _, sleeps = source(*rate_limited(2), files(file_meta("f1", "a.txt")), response(200, content=b"a"))
        assert [e.data for e in pull(src)] == [b"a"]
        assert sleeps == [1.0, 2.0]

    def test_quota_403_is_not_retried(self):
        src, _, sleeps = source(drive_error(403, "Daily Limit Exceeded", "dailyLimitExceeded"))
        with pytest.raises(PipelineError, match="403: Daily Limit Exceeded"):
            pull(src)
        assert sleeps == []

    def test_missing_folder(self):
        src, _, _ = source(drive_error(404, "File not found: nope.", "notFound"))
        with pytest.raises(PipelineError, match="404: File not found"):
            pull(src, folder_id="nope")

    def test_download_failure_mid_pull(self):
        src, _, _ = source(files(file_meta("f1", "a.txt"), file_meta("f2", "b.txt")), response(200, content=b"a"), drive_error(404, "gone", "notFound"))
        pulled = src.pull(CONFIG)
        assert next(pulled).data == b"a"
        with pytest.raises(PipelineError):
            next(pulled)


class TestConfigAndResources:
    @pytest.mark.parametrize(("config", "ok"), [({}, False), ({"access_token": ""}, False), ({"access_token": "ya29.x"}, True)])
    def test_validate_config_is_offline(self, config, ok):
        src, session, _ = source()
        assert src.validate_config(config) is ok
        assert session.calls == []

    def test_list_resources(self):
        src, session, _ = source(
            files({"id": "fold1", "name": "Reports", "mimeType": FOLDER_MIME}, file_meta("f1", "a.pdf", "application/pdf", 7)),
            response(200, {"drives": [{"id": "0AD", "name": "Marketing"}]}),
        )
        resources = src.list_resources(CONFIG)
        assert [(r.id, r.name, r.resource_type) for r in resources] == [
            ("fold1", "Reports", "folder"),
            ("f1", "a.pdf", "file"),
            ("0AD", "Marketing", "shared_drive"),
        ]
        assert resources[1].metadata["size"] == 7
        assert session.calls[0].params["orderBy"] == "folder,name"
        assert session.last.path == "/drives"

    def test_list_resources_without_credentials(self):
        src, _, _ = source()
        with pytest.raises(ConfigValidationError):
            src.list_resources({})
