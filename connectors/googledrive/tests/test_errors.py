"""Failure contract: every failure class carries core's canonical
``error_code`` (and ``retry_after`` for rate limits); the host's
``config["timeout_seconds"]`` bounds a whole call; concurrent calls share no
HTTP session."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import pytest
import requests
from aisquare.pipe import ERROR_CODES

from aisquare_pipe_googledrive import GoogleDriveToolkit, rest
from aisquare_pipe_googledrive.formats import DOCUMENT
from aisquare_pipe_googledrive.rest import (
    FOLDER_MIME,
    GoogleDriveAPIError,
    GoogleDriveRestClient,
)
from aisquare_pipe_googledrive.toolkit import error_code, time_budget

from .fakes import (
    CONFIG,
    TOKEN,
    FakeSession,
    drive_error,
    file_meta,
    harness,
    rate_limited,
    response,
)

GET = ("GOOGLEDRIVE_GET_FILE_METADATA", {"fileId": "file123"})
POST = ("GOOGLEDRIVE_CREATE_FOLDER", {"name": "Reports"})


def _retry_after(n: int, seconds: str, status: int = 429):
    return [
        drive_error(status, "Rate Limit Exceeded", "rateLimitExceeded", headers={"Retry-After": seconds})
        for _ in range(n)
    ]


# (id, tool call, scripted answers, error_code, retry_after)
FAILURES = [
    ("401", GET, [drive_error(401, "Invalid Credentials", "authError")], "unauthorized", None),
    ("403 userRateLimitExceeded, retries exhausted", GET, rate_limited(4), "rate_limited", 8.0),
    (
        "403 rateLimitExceeded, retries exhausted",
        GET,
        rate_limited(4, reason="rateLimitExceeded"),
        "rate_limited",
        8.0,
    ),
    ("429, retries exhausted", POST, rate_limited(4, status=429, reason="rateLimitExceeded"), "rate_limited", 8.0),
    ("429 Retry-After beyond the cap", GET, _retry_after(1, "120"), "rate_limited", 120.0),
    ("403 Retry-After, retries exhausted", GET, _retry_after(4, "5", 403), "rate_limited", 5.0),
    (
        "403 insufficientFilePermissions",
        GET,
        [drive_error(403, "The user does not have sufficient permissions for this file.", "insufficientFilePermissions")],
        "forbidden",
        None,
    ),
    ("403 dailyLimitExceeded", GET, [drive_error(403, "Daily Limit Exceeded", "dailyLimitExceeded")], "forbidden", None),
    (
        "403 storageQuotaExceeded",
        POST,
        [drive_error(403, "The user's Drive storage quota has been exceeded.", "storageQuotaExceeded")],
        "forbidden",
        None,
    ),
    ("404", GET, [drive_error(404, "File not found: file123.", "notFound")], "not_found", None),
    ("400", GET, [drive_error(400, "Invalid field selection foo", "invalidParameter")], "invalid_arguments", None),
    ("409", POST, [drive_error(409, "Conflict", "conflict")], "conflict", None),
    ("412", POST, [drive_error(412, "Precondition Failed", "conditionNotMet")], "conflict", None),
    ("500", POST, [drive_error(500, "Internal Error", "internalError")], "upstream_error", None),
    ("503 x4 on a GET", GET, [drive_error(503, "Backend Error", "backendError")] * 4, "upstream_error", None),
    ("504", POST, [response(504, content=b"")], "timeout", None),
    ("non-JSON 502 page", POST, [response(502, content=b"<html>Bad Gateway</html>", content_type="text/html")], "upstream_error", None),
    ("ReadTimeout", GET, [requests.ReadTimeout("read timed out")], "timeout", None),
    ("ConnectTimeout", GET, [requests.ConnectTimeout("connect timed out")], "timeout", None),
    ("ConnectionError", GET, [requests.ConnectionError("refused")], "upstream_error", None),
    ("200 that is not JSON", GET, [response(200, content=b"<html>", content_type="text/html")], "upstream_error", None),
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


ARGUMENT_ERRORS = [
    ("dot id", "GOOGLEDRIVE_GET_FILE_METADATA", {"fileId": ".."}, []),
    ("dot id via alias", "GOOGLEDRIVE_DOWNLOAD_FILE", {"file_id": "."}, []),
    ("blank id", "GOOGLEDRIVE_DELETE_FILE", {"fileId": "   "}, []),
    ("missing id", "GOOGLEDRIVE_GET_FILE_METADATA", {}, []),
    ("download of a folder", "GOOGLEDRIVE_DOWNLOAD_FILE", {"fileId": "f1"}, [response(200, file_meta("f1", "Docs", FOLDER_MIME, None))]),
    (
        "download over the size limit",
        "GOOGLEDRIVE_DOWNLOAD_FILE",
        {"fileId": "f1"},
        [response(200, file_meta("f1", "big.iso", "application/octet-stream", 200 * 1024 * 1024))],
    ),
    (
        "download of a Form",
        "GOOGLEDRIVE_DOWNLOAD_FILE",
        {"fileId": "f1"},
        [response(200, file_meta("f1", "Survey", "application/vnd.google-apps.form", None))],
    ),
    (
        "export of a stored file",
        "GOOGLEDRIVE_EXPORT_GOOGLE_WORKSPACE_FILE",
        {"fileId": "f1", "mimeType": "application/pdf"},
        [response(200, file_meta("f1", "a.pdf", "application/pdf"))],
    ),
    ("export format not in the enum", "GOOGLEDRIVE_EXPORT_GOOGLE_WORKSPACE_FILE", {"fileId": "f1", "mimeType": "video/mp4"}, []),
    (
        "bad base64 upload",
        "GOOGLEDRIVE_UPLOAD_FILE",
        {"file_to_upload": {"name": "a.bin", "file_content_bytes": "not base64!"}},
        [],
    ),
    ("upload with only a Composio s3key", "GOOGLEDRIVE_UPLOAD_FILE", {"file_to_upload": {"name": "a", "mimetype": "text/plain", "s3key": "k"}}, []),
    ("upload into a dot folder", "GOOGLEDRIVE_UPLOAD_FILE", {"file_to_upload": {"name": "a", "file_content_bytes": "aGk="}, "folder_to_upload_to": ".."}, []),
    (
        "text into Slides",
        "GOOGLEDRIVE_CREATE_FILE_FROM_TEXT",
        {"file_name": "x", "text_content": "y", "mime_type": "application/vnd.google-apps.presentation"},
        [],
    ),
    ("edit as a Google Doc", "GOOGLEDRIVE_EDIT_FILE", {"file_id": "f1", "content": "x", "mime_type": DOCUMENT}, []),
    ("move without a destination", "GOOGLEDRIVE_MOVE_FILE", {"file_id": "f1"}, []),
    ("empty metadata update", "GOOGLEDRIVE_UPDATE_FILE_PUT", {"fileId": "f1"}, []),
    ("share with a user and no email", "GOOGLEDRIVE_ADD_FILE_SHARING_PREFERENCE", {"file_id": "f1", "role": "reader", "type": "user"}, []),
    ("share with a domain and no domain", "GOOGLEDRIVE_CREATE_PERMISSION", {"file_id": "f1", "role": "reader", "type": "domain"}, []),
    (
        "owner without transfer_ownership",
        "GOOGLEDRIVE_ADD_FILE_SHARING_PREFERENCE",
        {"file_id": "f1", "role": "owner", "type": "user", "email_address": "a@b.co"},
        [],
    ),
    ("bad role", "GOOGLEDRIVE_ADD_FILE_SHARING_PREFERENCE", {"file_id": "f1", "role": "admin", "type": "user"}, []),
    (
        "folder parent that is a file",
        "GOOGLEDRIVE_CREATE_FOLDER",
        {"name": "x", "parent_id": "file123"},
        [response(200, {"id": "file123", "mimeType": "text/plain"})],
    ),
    (
        "folder parent name that matches nothing",
        "GOOGLEDRIVE_CREATE_FOLDER",
        {"name": "x", "parent_id": "Nowhere"},
        [drive_error(404, "File not found: Nowhere.", "notFound"), response(200, {"files": []})],
    ),
    (
        "folder parent name that matches several",
        "GOOGLEDRIVE_CREATE_FOLDER",
        {"name": "x", "parent_id": "Reports"},
        [drive_error(404, "File not found: Reports.", "notFound"), response(200, {"files": [{"id": "a1"}, {"id": "a2"}]})],
    ),
    ("schema violation", "GOOGLEDRIVE_FIND_FILE", {"pageSize": "ten"}, []),
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
    result = h.call("GOOGLEDRIVE_GET_ABOUT", {}, config={})
    assert result.error_code == "unauthorized"
    assert h.session.calls == []


def test_unknown_slug():
    assert harness().call("GOOGLEDRIVE_DELETE_EVERYTHING", {}).error_code == "unknown_tool"


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (GoogleDriveAPIError(429, "x", retry_after=1), "rate_limited"),
        (GoogleDriveAPIError(403, "x", reason="userRateLimitExceeded", retry_after=1), "rate_limited"),
        (GoogleDriveAPIError(403, "x", reason="insufficientPermissions"), "forbidden"),
        (GoogleDriveAPIError(0, "x", timed_out=True), "timeout"),
        (GoogleDriveAPIError(0, "x"), "upstream_error"),
        (GoogleDriveAPIError(410, "Gone"), "not_found"),
        (GoogleDriveAPIError(412, "x"), "conflict"),
        (GoogleDriveAPIError(400, "x"), "invalid_arguments"),
        (GoogleDriveAPIError(401, "x"), "unauthorized"),
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
    client = GoogleDriveRestClient(
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
        h = harness(response(200, {}))
        config = {**CONFIG, "timeout_seconds": budget} if budget is not None else CONFIG
        h.call("GOOGLEDRIVE_GET_ABOUT", {}, config=config)
        assert h.session.last.timeout == pytest.approx(timeout, abs=0.5)

    def test_download_body_timeout_is_capped_by_the_budget(self):
        h = harness(response(200, file_meta()), response(200, content=b"hello"))
        h.call("GOOGLEDRIVE_DOWNLOAD_FILE", {"fileId": "file123"}, config={**CONFIG, "timeout_seconds": 9})
        assert h.session.last.timeout == pytest.approx(9, abs=0.5)

    def test_download_body_gets_the_media_timeout_without_a_budget(self):
        h = harness(response(200, file_meta()), response(200, content=b"hello"))
        h.call("GOOGLEDRIVE_DOWNLOAD_FILE", {"fileId": "file123"})
        assert [c.timeout for c in h.session.calls] == [rest.DEFAULT_TIMEOUT, rest.MEDIA_TIMEOUT]

    def test_retry_sleep_never_outlasts_the_budget(self):
        h = harness(*_retry_after(1, "20"))
        result = h.call("GOOGLEDRIVE_GET_ABOUT", {}, config={**CONFIG, "timeout_seconds": 10})
        assert h.sleeps == []
        assert len(h.session.calls) == 1
        assert result.error_code == "rate_limited"
        assert result.retry_after == 20.0

    def test_remaining_budget_shrinks_each_attempt(self):
        client, session, clock = _budgeted(5, *_retry_after(1, "3"), response(200, {}))
        client.request("GET", "/about")
        assert clock.sleeps == [3.0]
        assert [c.timeout for c in session.calls] == [5.0, 2.0]

    def test_a_retry_that_would_outlast_the_budget_fails_now(self):
        client, session, clock = _budgeted(5, *_retry_after(2, "3"))
        with pytest.raises(GoogleDriveAPIError) as exc:
            client.request("GET", "/about")
        assert exc.value.retry_after == 3.0
        assert clock.sleeps == [3.0]  # the second 3s wait would pass the 5s budget
        assert len(session.calls) == 2

    def test_spent_budget_times_out_without_a_request(self):
        client, session, clock = _budgeted(5)
        clock.now = 5.0
        with pytest.raises(GoogleDriveAPIError) as exc:
            client.request("GET", "/about")
        assert exc.value.timed_out
        assert session.calls == []

    def test_request_timeout_names_the_seconds_not_the_url(self):
        h = harness(requests.ReadTimeout("HTTPSConnectionPool: /drive/v3/files?q=project-x-secret"))
        result = h.call("GOOGLEDRIVE_FIND_FILE", {"q": "project-x-secret"}, config={**CONFIG, "timeout_seconds": 7})
        assert result.error_code == "timeout"
        assert result.error == "Google Drive did not answer within 7s (ReadTimeout)"


# --------------------------------------------------------------------------
# Concurrency
# --------------------------------------------------------------------------


class TestConcurrency:
    def test_each_call_gets_its_own_session_and_closes_it(self):
        made: list[FakeSession] = []
        lock = threading.Lock()

        def new_session():
            session = FakeSession([response(200, {"user": {"displayName": "Ada"}})])
            with lock:
                made.append(session)
            return session

        toolkit = GoogleDriveToolkit()
        with (
            mock.patch.object(rest.requests, "Session", side_effect=new_session),
            ThreadPoolExecutor(max_workers=8) as pool,
        ):
            results = list(pool.map(lambda _: toolkit.call_tool("GOOGLEDRIVE_GET_ABOUT", {}, CONFIG), range(16)))
        assert all(r.successful for r in results)
        assert len(made) == 16
        assert len({id(s) for s in made}) == 16
        assert all(s.closed and len(s.calls) == 1 for s in made)

    def test_fresh_instances_in_parallel_do_not_cross_talk(self):
        def run(i: int):
            h = harness(response(200, file_meta(f"file{i:04d}", f"n{i}")))
            result = h.call("GOOGLEDRIVE_GET_FILE_METADATA", {"fileId": f"file{i:04d}"})
            return i, result.data["name"], h.session.last.path

        with ThreadPoolExecutor(max_workers=8) as pool:
            for i, name, path in pool.map(run, range(1, 33)):
                assert name == f"n{i}"
                assert path == f"/files/file{i:04d}"
