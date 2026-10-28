"""File bytes (plan §4.8): the download's host allowlist, redirects and
caps, and the v2 upload POST — at the client boundary."""

from __future__ import annotations

import pytest
import requests
import urllib3.exceptions

from aisquare_pipe_slack.rest import SlackAPIError, SlackWebClient, is_slack_upload_url

from .fakes import USER_TOKEN, FakeSession, response

FILE_URL = "https://files.slack.com/files-pri/T0123-F0123/download/report.pdf"
#: urlsplit sees files.slack.com; requests (urllib3) connects to evil.example.
BACKSLASH = "https://evil.example\\@files.slack.com/files-pri/x"
UPLOAD_URL = "https://files.slack.com/upload/v1/ABC123"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _client(*responses, **kwargs) -> tuple[SlackWebClient, FakeSession]:
    session = FakeSession(list(responses))
    return SlackWebClient(USER_TOKEN, session=session, sleep=lambda s: None, **kwargs), session  # type: ignore[arg-type]


def _raises(fn, *args, **kwargs) -> SlackAPIError:
    with pytest.raises(SlackAPIError) as exc:
        fn(*args, **kwargs)
    return exc.value


class TestDownload:
    def test_streams_the_bytes_with_the_token_to_files_slack_com(self):
        client, session = _client(response(200, content=b"%PDF-1.7 ...", headers={"Content-Type": "application/pdf"}))
        data, content_type = client.download(FILE_URL, max_bytes=1000)
        assert (data, content_type) == (b"%PDF-1.7 ...", "application/pdf")
        call = session.last
        assert (call.method, call.url, call.stream, call.allow_redirects) == ("GET", FILE_URL, True, False)
        assert call.headers["Authorization"] == f"Bearer {USER_TOKEN}"

    @pytest.mark.parametrize(
        "url",
        [
            "http://files.slack.com/files-pri/x",
            "https://evil.test/files-pri/x",
            "https://files.slack.com.evil.test/x",
            "https://slack-files.com/x",
            "ftp://files.slack.com/x",
            None,
            BACKSLASH,
            "https://files.slack.com@evil.test/x",
            "https://user:pw@files.slack.com/x",
            "https://files.slack.com:8443/x",
            "https://files.slack.com/x y",
            "https://files.slack.com/x\n",
            "https:files.slack.com/x",
        ],
    )
    def test_only_https_files_slack_com_gets_the_token(self, url):
        client, session = _client()
        error = _raises(client.download, url, max_bytes=10)
        assert error.error == "invalid_download_url"
        assert session.calls == []

    def test_a_redirect_elsewhere_drops_the_token(self):
        client, session = _client(
            response(302, headers={"Location": "https://cdn.example.test/blob?sig=1"}),
            response(200, content=b"bytes", headers={"Content-Type": "application/octet-stream"}),
        )
        assert client.download(FILE_URL, max_bytes=100)[0] == b"bytes"
        first, second = session.calls
        assert "Authorization" in first.headers
        assert "Authorization" not in second.headers
        assert second.url == "https://cdn.example.test/blob?sig=1"

    def test_a_redirect_back_to_files_slack_com_keeps_it(self):
        client, session = _client(
            response(302, headers={"Location": "/files-pri/T0123-F0123/other"}),
            response(200, content=b"ok"),
        )
        client.download(FILE_URL, max_bytes=10)
        assert session.last.url == "https://files.slack.com/files-pri/T0123-F0123/other"
        assert session.last.headers["Authorization"] == f"Bearer {USER_TOKEN}"

    @pytest.mark.parametrize(
        "location", [BACKSLASH, "https://files.slack.com@evil.test/x", "https://files.slack.com:8443/x"]
    )
    def test_a_disguised_redirect_never_gets_the_token(self, location):
        client, session = _client(response(302, headers={"Location": location}), response(200, content=b"x"))
        error = _raises(client.download, FILE_URL, max_bytes=10)
        assert error.error == "insecure_redirect"
        assert len(session.calls) == 1  # nothing was sent to the disguised host

    def test_a_disguised_redirect_from_an_off_host_hop(self):
        client, session = _client(
            response(302, headers={"Location": "https://cdn.example.test/blob"}),
            response(302, headers={"Location": BACKSLASH}),
        )
        assert _raises(client.download, FILE_URL, max_bytes=10).error == "insecure_redirect"
        assert len(session.calls) == 2
        assert "Authorization" not in session.calls[1].headers

    @pytest.mark.parametrize(
        "failure, code",
        [
            (requests.exceptions.ChunkedEncodingError("broken"), "network_error"),
            (requests.ConnectionError("reset"), "network_error"),
            (requests.ConnectionError(urllib3.exceptions.ReadTimeoutError(None, "/x", "read timed out")), "timeout"),
            (requests.ReadTimeout("slow"), "timeout"),
        ],
    )
    def test_a_transport_failure_mid_stream_is_classified(self, failure, code):
        class Broken(requests.Response):
            def iter_content(self, chunk_size=1, decode_unicode=False):
                yield b"partial"
                raise failure

        broken = Broken()
        broken._content_consumed = True
        broken.status_code = 200
        broken.headers = requests.structures.CaseInsensitiveDict({})
        client, _ = _client(broken)
        error = _raises(client.download, FILE_URL, max_bytes=1000)
        assert error.error == code
        assert error.timed_out is (code == "timeout")

    def test_redirects_must_stay_https_and_are_bounded(self):
        client, _ = _client(response(302, headers={"Location": "http://files.slack.com/x"}))
        assert _raises(client.download, FILE_URL, max_bytes=10).error == "insecure_redirect"
        loops = [response(302, headers={"Location": FILE_URL}) for _ in range(4)]
        client, session = _client(*loops)
        assert _raises(client.download, FILE_URL, max_bytes=10).error == "too_many_redirects"
        assert len(session.calls) == 4

    def test_the_cap_is_enforced_while_reading(self):
        client, _ = _client(response(200, content=b"x" * (200 * 1024)))
        error = _raises(client.download, FILE_URL, max_bytes=100 * 1024)
        assert error.error == "file_too_large"

    def test_the_budget_is_checked_between_reads(self):
        clock = Clock()

        class Slow(requests.Response):
            def iter_content(self, chunk_size=1, decode_unicode=False):
                for _ in range(3):
                    clock.now += 4
                    yield b"x" * 10

        slow = Slow()
        slow._content_consumed = True  # no raw stream to close
        slow.status_code = 200
        slow.headers = requests.structures.CaseInsensitiveDict({})
        client, _ = _client(slow, budget=5, monotonic=clock)
        assert _raises(client.download, FILE_URL, max_bytes=1000).timed_out

    @pytest.mark.parametrize("status, code", [(403, "http_403"), (404, "http_404"), (500, "http_500")])
    def test_http_failures(self, status, code):
        client, _ = _client(response(status, content=b"<html>"))
        error = _raises(client.download, FILE_URL, max_bytes=10)
        assert (error.error, error.status) == (code, status)

    def test_rate_limit_carries_retry_after(self):
        client, _ = _client(response(429, headers={"Retry-After": "7"}))
        error = _raises(client.download, FILE_URL, max_bytes=10)
        assert (error.status, error.retry_after) == (429, 7.0)

    def test_transport_errors_name_no_url(self):
        client, _ = _client(requests.ConnectionError(FILE_URL + "?token=" + USER_TOKEN))
        error = _raises(client.download, FILE_URL, max_bytes=10)
        assert error.error == "network_error" and USER_TOKEN not in str(error)


class TestUpload:
    def test_posts_raw_bytes_without_the_token(self):
        client, session = _client(response(200, content=b"OK - 5"))
        client.upload_external(UPLOAD_URL, b"hello")
        call = session.last
        assert (call.method, call.url, call.data, call.allow_redirects) == ("POST", UPLOAD_URL, b"hello", False)
        assert "Authorization" not in call.headers
        assert call.headers["Content-Type"] == "application/octet-stream"

    @pytest.mark.parametrize(
        "url, ok",
        [
            (UPLOAD_URL, True),
            ("https://slack.com/upload/x", True),
            ("http://files.slack.com/upload/x", False),
            ("https://evilslack.com/upload/x", False),
            ("https://files.slack.com.evil.test/x", False),
            ("https://169.254.169.254/latest", False),
            (BACKSLASH.replace("files-pri", "upload"), False),
            ("https://files.slack.com@evil.test/upload", False),
            ("https://files.slack.com:8443/upload", False),
        ],
    )
    def test_only_https_slack_hosts(self, url, ok):
        assert is_slack_upload_url(url) is ok
        client, session = _client(response(200))
        if ok:
            client.upload_external(url, b"x")
        else:
            assert _raises(client.upload_external, url, b"x").error == "invalid_upload_url"
            assert session.calls == []

    @pytest.mark.parametrize("status", [201, 302, 400, 500])
    def test_anything_but_200_fails(self, status):
        client, _ = _client(response(status, content=b"no"))
        assert _raises(client.upload_external, UPLOAD_URL, b"x").error == f"http_{status}"
