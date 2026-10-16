"""GoogleDriveRestClient: headers, error parsing, rate-limit retries, and the
multipart / resumable upload protocols."""

from __future__ import annotations

import io
import json
import logging
from unittest import mock

import pytest
import requests

from aisquare_pipe_googledrive import rest
from aisquare_pipe_googledrive.rest import (
    GoogleDriveAPIError,
    GoogleDriveRestClient,
    error_details,
    escape_query,
    is_rate_limited,
    multipart_body,
    quote_id,
    retry_wait,
)

from .fakes import SESSION_URL, TOKEN, FakeSession, drive_error, rate_limited, response


def client(*answers, **kwargs):
    session = FakeSession(list(answers))
    sleeps: list[float] = []
    return GoogleDriveRestClient(TOKEN, session=session, sleep=sleeps.append, **kwargs), session, sleeps  # type: ignore[arg-type]


class TestRequest:
    def test_sends_bearer_token_accept_and_user_agent(self):
        c, session, _ = client(response(200, {"ok": True}))
        c.get("/about", {"fields": "user"})
        call = session.last
        assert call.url == "https://www.googleapis.com/drive/v3/about"
        assert call.params == {"fields": "user"}
        assert call.headers == {
            "Accept": "application/json",
            "Authorization": f"Bearer {TOKEN}",
            "User-Agent": "aisquare-pipe-googledrive",
        }
        assert call.timeout == rest.DEFAULT_TIMEOUT

    def test_token_only_in_the_authorization_header(self):
        c, session, _ = client(response(200, {}))
        c.get("/files", {"q": "trashed = false"})
        call = session.last
        assert TOKEN not in call.url
        assert TOKEN not in json.dumps(call.params)
        assert TOKEN not in repr(c)

    def test_upload_requests_go_to_the_upload_base(self):
        c, session, _ = client(response(200, {}))
        c.request("POST", "/files", upload=True, data=b"x")
        assert session.last.url == "https://www.googleapis.com/upload/drive/v3/files"

    def test_owned_session_is_closed(self):
        owned = FakeSession()
        with mock.patch.object(rest.requests, "Session", return_value=owned), GoogleDriveRestClient(TOKEN):
            pass
        assert owned.closed is True

    def test_injected_session_is_left_open(self):
        c, session, _ = client()
        with c:
            pass
        assert session.closed is False


class TestErrors:
    def test_non_2xx_raises_with_status_message_and_reason(self):
        c, _, _ = client(drive_error(404, "File not found: abc.", "notFound"))
        with pytest.raises(GoogleDriveAPIError) as exc:
            c.get("/files/abc")
        assert (exc.value.status, exc.value.message, exc.value.reason) == (404, "File not found: abc.", "notFound")
        assert exc.value.retry_after is None
        assert str(exc.value) == "404: File not found: abc."

    def test_non_json_error_body_falls_back_to_the_reason_phrase(self):
        c, _, _ = client(response(502, content=b"<html>", content_type="text/html"))
        with pytest.raises(GoogleDriveAPIError) as exc:
            c.request("POST", "/files")
        assert (exc.value.status, exc.value.message, exc.value.reason) == (502, "Bad Gateway", None)

    def test_network_error_names_the_type_not_the_url(self):
        c, _, _ = client(requests.ConnectionError("Max retries exceeded with url: /files?q=secret-plan"))
        with pytest.raises(GoogleDriveAPIError) as exc:
            c.get("/files", {"q": "secret-plan"})
        assert exc.value.status == 0
        assert exc.value.message == "network error talking to Google Drive (ConnectionError)"
        assert "secret-plan" not in str(exc.value)

    def test_logs_carry_no_token_path_or_arguments(self, caplog):
        c, _, _ = client(*rate_limited(1), drive_error(404, "nope", "notFound"))
        with caplog.at_level(logging.DEBUG, logger="aisquare.pipe.googledrive"), pytest.raises(GoogleDriveAPIError):
            c.get("/files/secret-file-id", {"q": "name = 'secret-plan'"})
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert caplog.records
        for secret in (TOKEN, "secret-file-id", "secret-plan"):
            assert secret not in text


class TestRetry:
    def test_user_rate_limit_backs_off_then_succeeds(self):
        c, session, sleeps = client(*rate_limited(2), response(200, {"ok": True}))
        assert c.get("/about").json() == {"ok": True}
        assert sleeps == [1.0, 2.0]
        assert len(session.calls) == 3

    def test_retry_after_header_wins_over_backoff(self):
        c, _, sleeps = client(drive_error(429, "slow down", "rateLimitExceeded", headers={"Retry-After": "7"}), response(200, {}))
        c.get("/about")
        assert sleeps == [7.0]

    @pytest.mark.parametrize("reason", ["insufficientFilePermissions", "dailyLimitExceeded", "storageQuotaExceeded", "domainPolicy"])
    def test_other_403s_are_not_retried(self, reason):
        c, session, sleeps = client(drive_error(403, "no", reason))
        with pytest.raises(GoogleDriveAPIError) as exc:
            c.get("/about")
        assert exc.value.retry_after is None
        assert (sleeps, len(session.calls)) == ([], 1)

    def test_retries_are_bounded(self):
        c, session, sleeps = client(*rate_limited(4))
        with pytest.raises(GoogleDriveAPIError) as exc:
            c.get("/about")
        assert exc.value.retry_after == 8.0
        assert sleeps == [1.0, 2.0, 4.0]
        assert len(session.calls) == 4

    def test_wait_beyond_the_budget_fails_fast(self):
        c, session, sleeps = client(
            drive_error(429, "slow", "rateLimitExceeded", headers={"Retry-After": "90"}), max_retry_wait=60
        )
        with pytest.raises(GoogleDriveAPIError) as exc:
            c.get("/about")
        assert exc.value.retry_after == 90.0
        assert (sleeps, len(session.calls)) == ([], 1)

    def test_total_sleep_stays_within_the_budget(self):
        c, _, sleeps = client(*rate_limited(4), max_retries=10, max_retry_wait=5)
        with pytest.raises(GoogleDriveAPIError):
            c.get("/about")
        assert sleeps == [1.0, 2.0]  # the next 4s wait would pass 5s in total

    def test_server_error_on_get_is_retried(self):
        c, _, sleeps = client(drive_error(503, "Backend Error", "backendError"), response(200, {}))
        c.get("/about")
        assert sleeps == [1.0]

    def test_server_error_on_post_is_not_retried(self):
        c, session, sleeps = client(drive_error(500, "Internal Error", "internalError"))
        with pytest.raises(GoogleDriveAPIError) as exc:
            c.request("POST", "/files", json={"name": "x"})
        assert exc.value.retry_after is None
        assert (sleeps, len(session.calls)) == ([], 1)

    def test_rate_limit_on_a_post_is_retried(self):
        c, session, sleeps = client(*rate_limited(1), response(200, {"id": "x"}))
        c.request("POST", "/files", json={"name": "x"})
        assert (sleeps, len(session.calls)) == ([1.0], 2)

    def test_streamed_error_response_is_closed(self):
        failed = drive_error(404, "nope", "notFound")
        closed: list[bool] = []
        failed.close = lambda: closed.append(True)  # type: ignore[method-assign]
        c, _, _ = client(failed)
        with pytest.raises(GoogleDriveAPIError):
            c.download("abc", stream=True)
        assert closed == [True]


class TestHelpers:
    def test_error_details(self):
        assert error_details(drive_error(403, "Rate", "userRateLimitExceeded")) == ("Rate", "userRateLimitExceeded")
        assert error_details(response(400, {"error": {"code": 400, "message": "Bad"}})) == ("Bad", None)
        assert error_details(response(400, {"error": "invalid_grant"})) == ("Bad Request", None)

    @pytest.mark.parametrize(
        ("answer", "limited"),
        [
            (drive_error(429, "x", "rateLimitExceeded"), True),
            (response(429, content=b""), True),
            (drive_error(403, "x", "rateLimitExceeded"), True),
            (drive_error(403, "x", "userRateLimitExceeded"), True),
            (drive_error(403, "x", "dailyLimitExceeded"), False),
            (drive_error(403, "x", "insufficientPermissions"), False),
            (drive_error(500, "x", "backendError"), False),
        ],
    )
    def test_is_rate_limited(self, answer, limited):
        assert is_rate_limited(answer) is limited

    def test_retry_wait(self):
        assert [retry_wait(response(429, content=b""), n) for n in range(4)] == [1.0, 2.0, 4.0, 8.0]
        assert retry_wait(response(429, content=b"", headers={"Retry-After": "12"}), 3) == 12.0
        assert retry_wait(response(429, content=b"", headers={"Retry-After": "soon"}), 0) == 1.0

    @pytest.mark.parametrize("bad", ["", " ", ".", ".."])
    def test_quote_id_refuses_dot_segments(self, bad):
        with pytest.raises(ValueError):
            quote_id(bad)

    def test_quote_id_encodes_everything_else(self):
        assert quote_id("1AbC_-x") == "1AbC_-x"
        assert quote_id("a/../b?x=1") == "a%2F..%2Fb%3Fx%3D1"

    def test_escape_query(self):
        assert escape_query("it's a \\ test") == "it\\'s a \\\\ test"

    def test_multipart_body(self):
        body, header = multipart_body({"name": "a.txt"}, b"hello", "text/plain", "BOUND")
        assert header == "multipart/related; boundary=BOUND"
        assert body == (
            b"--BOUND\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            b'{"name": "a.txt"}\r\n--BOUND\r\nContent-Type: text/plain\r\n\r\nhello\r\n--BOUND--\r\n'
        )


class TestDownload:
    def test_stored_file_uses_alt_media(self):
        c, session, _ = client(response(200, content=b"x"))
        c.download("abc")
        assert session.last.params == {"alt": "media", "supportsAllDrives": "true"}
        assert session.last.headers["Accept"] == "*/*"
        assert session.last.timeout == rest.MEDIA_TIMEOUT

    def test_native_file_is_exported(self):
        c, session, _ = client(response(200, content=b"x"))
        c.download("abc", export_mime="text/csv", stream=True)
        assert session.last.url.endswith("/files/abc/export")
        assert session.last.params == {"mimeType": "text/csv"}
        assert session.last.stream is True


def _started(url: str = SESSION_URL):
    return response(200, content=b"", headers={"Location": url})


def _incomplete(received: int | None):
    headers = {"Range": f"bytes=0-{received - 1}"} if received else {}
    return response(308, content=b"", headers=headers)


@pytest.fixture
def small_chunks(monkeypatch):
    """4-byte chunks; anything over 8 bytes goes resumable."""
    monkeypatch.setattr(rest, "CHUNK_SIZE", 4)
    monkeypatch.setattr(rest, "MULTIPART_MAX", 8)


class TestUpload:
    def test_small_content_is_one_multipart_request(self):
        c, session, _ = client(response(200, {"id": "new", "name": "a.txt"}))
        created = c.upload(b"hello", metadata={"name": "a.txt"}, content_type="text/plain", params={"fields": "id,name"})
        assert created == {"id": "new", "name": "a.txt"}
        call = session.last
        assert (call.method, call.upload, call.path) == ("POST", True, "/files")
        assert call.params == {"supportsAllDrives": "true", "fields": "id,name", "uploadType": "multipart"}
        assert call.headers["Content-Type"].startswith("multipart/related; boundary=aisquare-pipe-")
        assert b'{"name": "a.txt"}' in call.data and b"\r\n\r\nhello\r\n" in call.data

    def test_replacing_content_patches_the_file(self):
        c, session, _ = client(response(200, {"id": "f1"}))
        c.upload(b"v2", metadata={}, content_type="text/plain", file_id="f1")
        assert (session.last.method, session.last.path) == ("PATCH", "/files/f1")

    def test_small_stream_of_known_size_is_multipart(self):
        c, session, _ = client(response(200, {"id": "new"}))
        c.upload(io.BytesIO(b"hello"), metadata={"name": "a"}, content_type="text/plain", size=5)
        assert session.last.params["uploadType"] == "multipart"

    def test_large_content_goes_resumable_in_chunks(self, small_chunks):
        c, session, _ = client(_started(), _incomplete(4), _incomplete(8), response(200, {"id": "big"}))
        created = c.upload(b"0123456789", metadata={"name": "big.bin"}, content_type="application/octet-stream")
        assert created == {"id": "big"}
        start, *puts = session.calls
        assert (start.method, start.upload, start.params["uploadType"]) == ("POST", True, "resumable")
        assert start.json == {"name": "big.bin"}
        assert start.headers["X-Upload-Content-Type"] == "application/octet-stream"
        assert start.headers["X-Upload-Content-Length"] == "10"
        assert [(p.method, p.url) for p in puts] == [("PUT", SESSION_URL)] * 3
        assert [p.headers["Content-Range"] for p in puts] == ["bytes 0-3/10", "bytes 4-7/10", "bytes 8-9/10"]
        assert [p.data for p in puts] == [b"0123", b"4567", b"89"]
        assert all("Authorization" not in p.headers for p in puts)
        assert all(p.allow_redirects is False for p in puts)

    def test_partial_chunk_is_resent_from_where_google_stopped(self, small_chunks):
        c, session, _ = client(_started(), _incomplete(2), _incomplete(4), response(201, {"id": "big"}))
        c.upload(b"012345678", metadata={}, content_type="text/plain")
        puts = session.calls[1:]
        assert [p.headers["Content-Range"] for p in puts] == ["bytes 0-3/9", "bytes 2-3/9", "bytes 4-7/9"]
        assert [p.data for p in puts] == [b"0123", b"23", b"4567"]

    def test_stream_of_unknown_size_announces_the_total_at_the_end(self, small_chunks):
        c, session, _ = client(_started(), _incomplete(4), response(200, {"id": "s"}))
        c.upload(io.BytesIO(b"abcdef"), metadata={"name": "s"}, content_type="text/plain")
        start, *puts = session.calls
        assert "X-Upload-Content-Length" not in start.headers
        assert [p.headers["Content-Range"] for p in puts] == ["bytes 0-3/*", "bytes 4-5/6"]

    def test_empty_stream_finalizes_with_an_empty_put(self, small_chunks):
        c, session, _ = client(_started(), response(200, {"id": "e"}))
        c.upload(io.BytesIO(b""), metadata={"name": "e"}, content_type="text/plain")
        assert session.last.headers["Content-Range"] == "bytes */0"
        assert session.last.data == b""

    def test_rate_limited_chunk_is_retried(self, small_chunks):
        c, session, sleeps = client(_started(), *rate_limited(1, status=429, reason="rateLimitExceeded"), response(200, {"id": "x"}))
        c.upload(b"012345678", metadata={}, content_type="text/plain", size=None)
        assert sleeps == [1.0]
        assert [p.headers["Content-Range"] for p in session.calls[1:]] == ["bytes 0-3/9", "bytes 0-3/9"]

    @pytest.mark.parametrize("location", ["", "http://www.googleapis.com/upload/x", "https://evil.example/upload"])
    def test_foreign_session_url_is_refused(self, small_chunks, location):
        c, session, _ = client(_started(location))
        with pytest.raises(GoogleDriveAPIError) as exc:
            c.upload(b"0123456789", metadata={}, content_type="text/plain")
        assert "upload session URL" in exc.value.message
        assert len(session.calls) == 1

    def test_stalled_session_gives_up(self, small_chunks):
        c, session, _ = client(_started(), *[_incomplete(None) for _ in range(4)])
        with pytest.raises(GoogleDriveAPIError) as exc:
            c.upload(b"0123456789", metadata={}, content_type="text/plain")
        assert "stopped accepting data" in exc.value.message
        assert len(session.calls) == 5

    def test_range_beyond_what_was_sent_is_an_error(self, small_chunks):
        c, _, _ = client(_started(), _incomplete(7))
        with pytest.raises(GoogleDriveAPIError) as exc:
            c.upload(b"0123456789", metadata={}, content_type="text/plain")
        assert "lost track" in exc.value.message

    def test_session_that_never_returns_the_file_is_an_error(self, small_chunks):
        c, _, _ = client(_started(), _incomplete(4), _incomplete(8), _incomplete(10))
        with pytest.raises(GoogleDriveAPIError) as exc:
            c.upload(b"0123456789", metadata={}, content_type="text/plain")
        assert "without returning the file" in exc.value.message

    def test_chunk_failure_carries_googles_error(self, small_chunks):
        c, _, _ = client(_started(), drive_error(400, "Invalid Content-Range", "badContent"))
        with pytest.raises(GoogleDriveAPIError) as exc:
            c.upload(b"0123456789", metadata={}, content_type="text/plain")
        assert (exc.value.status, exc.value.reason) == (400, "badContent")


class TestReviewFollowUps:
    """PR #37 review items applied to this client: retries leave room for the
    request (m2), any error-body shape parses (m3), downloads are capped and
    watched against the deadline chunk by chunk (n2)."""

    def _budgeted(self, budget, *answers):
        clock = {"now": 0.0}
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock["now"] += seconds

        session = FakeSession(list(answers))
        c = GoogleDriveRestClient(TOKEN, session=session, budget=budget, sleep=sleep, monotonic=lambda: clock["now"])  # type: ignore[arg-type]
        return c, session, sleeps, clock

    @pytest.mark.parametrize("retry_after", ["4", "5"])
    def test_a_sleep_that_leaves_no_room_for_the_retry_fails_rate_limited(self, retry_after):
        c, session, sleeps, _ = self._budgeted(
            5, drive_error(429, "slow", "rateLimitExceeded", headers={"Retry-After": retry_after})
        )
        with pytest.raises(GoogleDriveAPIError) as exc:
            c.get("/about")
        assert exc.value.retry_after == float(retry_after)
        assert exc.value.timed_out is False
        assert (sleeps, len(session.calls)) == ([], 1)

    def test_a_sleep_with_room_left_is_taken(self):
        c, session, sleeps, _ = self._budgeted(
            5, drive_error(429, "slow", "rateLimitExceeded", headers={"Retry-After": "3"}), response(200, {})
        )
        c.get("/about")
        assert sleeps == [3.0]
        assert [call.timeout for call in session.calls] == [5.0, 2.0]

    @pytest.mark.parametrize(
        "body",
        [
            {"error": "invalid_request"},
            {"error": {"message": {"nested": True}, "errors": {"reason": "x"}}},
            {"error": {"errors": [1, "a", None, {"reason": 7}]}},
            {"error": {"code": 400, "errors": "notalist"}},
            [1, 2, 3],
            5,
            "text",
            None,
        ],
    )
    def test_any_error_body_shape_parses(self, body):
        for status in (400, 403, 429):
            c, _, _ = client(response(status, body), *([response(status, body)] * 3))
            with pytest.raises(GoogleDriveAPIError) as exc:
                c.request("POST", "/files")
            assert exc.value.status == status
            assert isinstance(exc.value.message, str) and exc.value.message

    def test_download_over_the_cap_stops_reading(self):
        answer = response(200, content=b"x" * 200_000)
        c, _, _ = client(answer)
        with pytest.raises(rest.DownloadLimitError) as exc:
            c.download_bytes("abc", max_bytes=100_000)
        assert exc.value.limit == 100_000
        assert answer.raw.closed  # stopped mid-body and released

    def test_download_within_the_cap(self):
        c, session, _ = client(response(200, content=b"hello"))
        assert c.download_bytes("abc", export_mime="text/plain", max_bytes=5) == b"hello"
        assert session.last.stream is True

    def test_spent_budget_between_chunks_times_out(self):
        c, _, _, clock = self._budgeted(5)
        answer = response(200, content=b"x" * (3 * rest._READ_CHUNK))

        class SlowRaw(io.BytesIO):
            def read(self, n=-1):
                clock["now"] += 2.0
                return super().read(n)

        answer.raw = SlowRaw(b"x" * (3 * rest._READ_CHUNK))
        c._session.queue(answer)  # type: ignore[attr-defined]
        with pytest.raises(GoogleDriveAPIError) as exc:
            c.download_bytes("abc", max_bytes=10**9)
        assert exc.value.timed_out is True

    @pytest.mark.parametrize(
        ("error", "timed_out"), [(requests.ReadTimeout("slow"), True), (requests.ConnectionError("reset"), False)]
    )
    def test_transport_failure_mid_body(self, error, timed_out):
        answer = response(200, content=b"abc")

        class BrokenRaw(io.BytesIO):
            def read(self, n=-1):
                raise error

        answer.raw = BrokenRaw()
        c, _, _ = client(answer)
        with pytest.raises(GoogleDriveAPIError) as exc:
            c.download_bytes("abc", max_bytes=10)
        assert exc.value.timed_out is timed_out
        assert "abc" not in exc.value.message


class TestResumeAfterChunkFailure:
    """PR #39 review m2: a 5xx or a dropped connection on a chunk does not end
    the upload — the client asks Google how much it holds and carries on."""

    def test_503_on_chunk_two_resumes(self, small_chunks):
        c, session, sleeps = client(
            _started(),
            _incomplete(4),
            drive_error(503, "Backend Error", "backendError"),
            _incomplete(4),
            _incomplete(8),
            response(200, {"id": "big"}),
        )
        assert c.upload(b"0123456789", metadata={}, content_type="text/plain") == {"id": "big"}
        puts = session.calls[1:]
        assert [p.headers["Content-Range"] for p in puts] == [
            "bytes 0-3/10",
            "bytes 4-7/10",
            "bytes */10",
            "bytes 4-7/10",
            "bytes 8-9/10",
        ]
        assert puts[2].data == b""
        assert all("Authorization" not in p.headers and p.allow_redirects is False for p in puts)
        assert sleeps == [1.0]

    def test_connection_reset_resumes_from_what_google_holds(self, small_chunks):
        c, session, sleeps = client(
            _started(), requests.ConnectionError("reset by peer"), _incomplete(2), _incomplete(4), _incomplete(8), response(201, {"id": "x"})
        )
        c.upload(b"0123456789", metadata={}, content_type="text/plain")
        assert [p.headers["Content-Range"] for p in session.calls[1:]] == [
            "bytes 0-3/10",
            "bytes */10",
            "bytes 2-3/10",
            "bytes 4-7/10",
            "bytes 8-9/10",
        ]
        assert sleeps == [1.0]

    def test_unknown_size_status_query_says_so(self, small_chunks):
        c, session, _ = client(_started(), drive_error(502, "Bad Gateway", "backendError"), _incomplete(4), _incomplete(8), response(200, {"id": "s"}))
        c.upload(io.BytesIO(b"0123456789"), metadata={}, content_type="text/plain")
        assert [p.headers["Content-Range"] for p in session.calls[1:3]] == ["bytes 0-3/*", "bytes */*"]

    def test_upload_that_completed_despite_the_error_returns_the_file(self, small_chunks):
        c, session, _ = client(_started(), _incomplete(4), _incomplete(8), requests.ReadTimeout("slow"), response(200, {"id": "done"}))
        assert c.upload(b"0123456789", metadata={}, content_type="text/plain") == {"id": "done"}
        assert session.last.headers["Content-Range"] == "bytes */10"

    def test_resumes_are_bounded_by_max_retries(self, small_chunks):
        failure = drive_error(503, "Backend Error", "backendError")
        c, session, sleeps = client(_started(), failure, failure, failure, failure)
        with pytest.raises(GoogleDriveAPIError) as exc:
            c.upload(b"0123456789", metadata={}, content_type="text/plain")
        assert exc.value.status == 503
        assert sleeps == [1.0, 2.0, 4.0]
        assert len(session.calls) == 5

    def test_progress_resets_the_failure_count(self, small_chunks):
        failure = drive_error(503, "Backend Error", "backendError")
        c, _, sleeps = client(
            _started(),
            failure, _incomplete(4),  # chunk 1 fails once, then lands
            failure, _incomplete(8),  # chunk 2 fails once, then lands
            failure, response(200, {"id": "x"}),  # chunk 3 fails, the query finds it done
        )
        assert c.upload(b"0123456789", metadata={}, content_type="text/plain") == {"id": "x"}
        assert sleeps == [1.0, 1.0, 1.0]

    @pytest.mark.parametrize("status", [400, 403, 404])
    def test_client_errors_are_not_resumed(self, small_chunks, status):
        c, session, sleeps = client(_started(), drive_error(status, "no", "badRequest"))
        with pytest.raises(GoogleDriveAPIError) as exc:
            c.upload(b"0123456789", metadata={}, content_type="text/plain")
        assert (exc.value.status, sleeps, len(session.calls)) == (status, [], 2)

    def test_no_resume_when_the_budget_cannot_fit_it(self, small_chunks):
        clock = {"now": 0.0}
        session = FakeSession([_started(), drive_error(503, "Backend Error", "backendError")])
        sleeps: list[float] = []
        c = GoogleDriveRestClient(TOKEN, session=session, budget=2.5, sleep=sleeps.append, monotonic=lambda: clock["now"])  # type: ignore[arg-type]
        with pytest.raises(GoogleDriveAPIError) as exc:
            c.upload(b"0123456789", metadata={}, content_type="text/plain")
        assert exc.value.status == 503
        assert sleeps == []

    def test_query_answer_behind_the_confirmed_offset_is_an_error(self, small_chunks):
        c, _, _ = client(_started(), _incomplete(4), drive_error(503, "x", "backendError"), _incomplete(2))
        with pytest.raises(GoogleDriveAPIError) as exc:
            c.upload(b"0123456789", metadata={}, content_type="text/plain")
        assert "lost track" in exc.value.message
