"""GoogleDriveSink: uploads (multipart and resumable), target folders,
conflict handling and Drive failures — HTTP mocked at the client boundary."""

from __future__ import annotations

import io
import json

import pytest
from aisquare.pipe import DataEnvelope, PushParams

from aisquare_pipe_googledrive import GoogleDriveSink, rest
from aisquare_pipe_googledrive.rest import FOLDER_MIME

from .fakes import CONFIG, SESSION_URL, FakeSession, drive_error, rate_limited, response

UPLOADED = {"id": "new1", "name": "a.txt", "mimeType": "text/plain", "size": "11", "webViewLink": "https://d/new1", "modifiedTime": "2026-10-16T00:00:00Z"}


def sink(*answers):
    session = FakeSession(list(answers))
    sleeps: list[float] = []
    return GoogleDriveSink(session=session, sleep=sleeps.append), session, sleeps  # type: ignore[arg-type]


def envelope(data=b"hello world", content_type="text/plain", filename="a.txt", stream=None, **meta):
    return DataEnvelope(content_type=content_type, data=data, source_id="test", metadata={"filename": filename, **meta}, stream=stream)


def files(*items):
    return response(200, {"files": list(items)})


def conflict_q(folder: str, name: str) -> str:
    return f"'{folder}' in parents and name = '{name}' and mimeType != '{FOLDER_MIME}' and trashed = false"


def push(snk, env=None, **params):
    return snk.push(env or envelope(), CONFIG, PushParams(params))


class TestPush:
    def test_small_file_is_one_multipart_upload_to_root(self):
        snk, session, _ = sink(files(), response(200, UPLOADED))
        result = push(snk)
        assert result.success, result.error
        assert result.ref == "new1"
        assert result.metadata == {
            "file_id": "new1",
            "name": "a.txt",
            "mime_type": "text/plain",
            "size": 11,
            "web_view_link": "https://d/new1",
            "modified_time": "2026-10-16T00:00:00Z",
            "folder_id": "root",
            "conflict": "created",
        }
        lookup, upload = session.calls
        assert lookup.params["q"] == conflict_q("root", "a.txt")
        assert (upload.method, upload.upload, upload.path, upload.params["uploadType"]) == ("POST", True, "/files", "multipart")
        assert b'{"name": "a.txt", "parents": ["root"]}' in upload.data
        assert b"Content-Type: text/plain\r\n\r\nhello world\r\n" in upload.data

    def test_folder_path_is_created_when_missing(self):
        snk, session, _ = sink(
            files({"id": "rep1", "name": "Reports"}),
            files(),
            response(200, {"id": "q3new"}),
            files(),
            response(200, UPLOADED),
        )
        result = push(snk, path="Reports/Q3", folder_id="fold1")
        assert result.success, result.error
        create = session.calls[2]
        assert (create.method, create.path) == ("POST", "/files")
        assert create.json == {"name": "Q3", "mimeType": FOLDER_MIME, "parents": ["rep1"]}
        assert session.calls[0].params["q"].startswith("'fold1' in parents and name = 'Reports'")
        assert result.metadata["folder_id"] == "q3new"
        assert b'"parents": ["q3new"]' in session.last.data

    def test_missing_folder_without_create_folders_fails(self):
        snk, _, _ = sink(files())
        result = push(snk, path="Nope", create_folders=False)
        assert result.success is False
        assert "not found" in result.error

    def test_conflict_fail(self):
        snk, session, _ = sink(files({"id": "old1", "name": "a.txt"}))
        result = push(snk, conflict="fail")
        assert result.success is False
        assert "already exists" in result.error
        assert len(session.calls) == 1

    def test_conflict_replace_uploads_a_new_revision(self):
        snk, session, _ = sink(files({"id": "old1", "name": "a.txt"}), response(200, {**UPLOADED, "id": "old1"}))
        result = push(snk, conflict="replace")
        assert (result.ref, result.metadata["conflict"]) == ("old1", "replaced")
        upload = session.last
        assert (upload.method, upload.path) == ("PATCH", "/files/old1")
        assert b"charset=UTF-8\r\n\r\n{}\r\n" in upload.data

    def test_conflict_rename_picks_a_free_name(self):
        snk, session, _ = sink(
            files({"id": "old1", "name": "a.txt"}),
            files({"name": "a.txt"}, {"name": "a (1).txt"}, {"name": "abc.txt"}),
            response(200, {**UPLOADED, "name": "a (2).txt"}),
        )
        result = push(snk)
        assert (result.metadata["name"], result.metadata["conflict"]) == ("a (2).txt", "renamed")
        assert session.calls[1].params["q"] == "'root' in parents and name contains 'a' and trashed = false"
        assert b'{"name": "a (2).txt", "parents": ["root"]}' in session.last.data

    def test_keep_both_skips_the_lookup(self):
        snk, session, _ = sink(response(200, UPLOADED))
        assert push(snk, conflict="keep_both").success
        assert len(session.calls) == 1

    def test_unknown_conflict_mode(self):
        snk, session, _ = sink()
        result = push(snk, conflict="overwrite")
        assert result.success is False and "conflict must be one of" in result.error
        assert session.calls == []

    def test_convert_to_and_description(self):
        snk, session, _ = sink(files(), response(200, UPLOADED))
        push(snk, envelope(b"PK..", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "r.docx"), convert_to="application/vnd.google-apps.document", description="Quarterly")
        assert b'{"name": "r.docx", "parents": ["root"], "description": "Quarterly", "mimeType": "application/vnd.google-apps.document"}' in session.last.data

    @pytest.mark.parametrize(
        ("data", "expected"),
        [("héllo", "héllo".encode()), ({"a": "é"}, json.dumps({"a": "é"}, ensure_ascii=False).encode())],
    )
    def test_text_and_json_payloads(self, data, expected):
        snk, session, _ = sink(response(200, UPLOADED))
        push(snk, envelope(data, "application/json"), conflict="keep_both")
        assert b"\r\n\r\n" + expected + b"\r\n--" in session.last.data

    def test_missing_filename_and_content_type_fall_back(self):
        snk, session, _ = sink(response(200, UPLOADED))
        env = DataEnvelope(content_type="", data=b"x", source_id="t", metadata={})
        snk.push(env, CONFIG, PushParams({"conflict": "keep_both"}))
        assert b'{"name": "unnamed_file", "parents": ["root"]}' in session.last.data
        assert b"Content-Type: application/octet-stream" in session.last.data

    def test_large_stream_goes_resumable(self, monkeypatch):
        monkeypatch.setattr(rest, "CHUNK_SIZE", 4)
        monkeypatch.setattr(rest, "MULTIPART_MAX", 8)
        snk, session, _ = sink(
            files(),
            response(200, content=b"", headers={"Location": SESSION_URL}),
            response(308, content=b"", headers={"Range": "bytes=0-3"}),
            response(308, content=b"", headers={"Range": "bytes=0-7"}),
            response(200, {**UPLOADED, "size": "10"}),
        )
        env = envelope(b"", "application/octet-stream", "big.bin", stream=io.BytesIO(b"0123456789"), size=10)
        result = push(snk, env)
        assert result.success, result.error
        start = session.calls[1]
        assert start.params["uploadType"] == "resumable"
        assert start.headers["X-Upload-Content-Length"] == "10"
        assert [c.headers["Content-Range"] for c in session.calls[2:]] == ["bytes 0-3/10", "bytes 4-7/10", "bytes 8-9/10"]
        assert snk.max_size() == 5 * 1024**4


class TestPushFailures:
    def test_no_credentials(self):
        snk, session, _ = sink()
        result = snk.push(envelope(), {})
        assert result.success is False and "not connected" in result.error
        assert session.calls == []

    def test_rejected_token_says_reconnect(self):
        snk, _, _ = sink(drive_error(401, "Invalid Credentials", "authError"))
        result = push(snk)
        assert result.success is False
        assert "must be reconnected" in result.error
        assert result.metadata == {"status": 401, "error_code": "unauthorized"}

    def test_rate_limit_is_retried(self):
        snk, _, sleeps = sink(files(), *rate_limited(1), response(200, UPLOADED))
        assert push(snk).success
        assert sleeps == [1.0]

    def test_rate_limit_that_persists(self):
        snk, _, _ = sink(*rate_limited(4))
        result = push(snk)
        assert result.success is False
        assert result.metadata == {"status": 403, "error_code": "rate_limited", "retry_after": 8.0}

    def test_storage_quota(self):
        snk, _, _ = sink(files(), drive_error(403, "The user's Drive storage quota has been exceeded.", "storageQuotaExceeded"))
        result = push(snk)
        assert result.success is False
        assert result.metadata["error_code"] == "forbidden"

    def test_unsupported_data(self):
        snk, session, _ = sink()
        env = DataEnvelope(content_type="text/plain", data=[1, 2], source_id="t", metadata={"filename": "x"})  # type: ignore[arg-type]
        result = snk.push(env, CONFIG)
        assert result.success is False and "unsupported envelope data type" in result.error
        assert session.calls == []

    def test_unexpected_crash_is_reported_not_raised(self):
        snk, _, _ = sink(RuntimeError("boom with a secret"))
        result = push(snk)
        assert result.success is False
        assert result.error == "Google Drive push failed (RuntimeError)"

    def test_validate_config_is_offline(self):
        snk, session, _ = sink()
        assert snk.validate_config({}) is False
        assert snk.validate_config(CONFIG) is True
        assert session.calls == []
