"""GitHubRestClient — headers, error parsing, rate-limit retries, pagination hints.

HTTP is a FakeSession (see tests/fakes.py); sleeping and the clock are
injected, so retry waits are asserted without waiting.
"""

from __future__ import annotations

import logging
from unittest import mock

import pytest
import requests

from aisquare_pipe_github import rest
from aisquare_pipe_github.rest import (
    API_VERSION,
    JSON_MEDIA,
    GitHubAPIError,
    GitHubRestClient,
    error_message,
    json_body,
    page_hint,
    rate_limit_wait,
    revoke_grant,
)

from .fakes import TOKEN, FakeSession, next_link, response

NOW = 1_800_000_000.0


def _client(*responses, **kwargs) -> tuple[GitHubRestClient, FakeSession, list[float]]:
    session = FakeSession(list(responses))
    sleeps: list[float] = []
    kwargs.setdefault("clock", lambda: NOW)
    client = GitHubRestClient(TOKEN, session=session, sleep=sleeps.append, **kwargs)  # type: ignore[arg-type]
    return client, session, sleeps


class TestRequest:
    def test_sends_bearer_token_api_version_and_accept(self):
        client, session, _ = _client(response(200, {"login": "octocat"}))
        client.request("GET", "/user", params={"page": 1})
        call = session.last
        assert call.method == "GET"
        assert call.url == "https://api.github.com/user"
        assert call.params == {"page": 1}
        assert call.headers["Authorization"] == f"Bearer {TOKEN}"
        assert call.headers["X-GitHub-Api-Version"] == API_VERSION == "2022-11-28"
        assert call.headers["Accept"] == JSON_MEDIA
        assert call.headers["User-Agent"]
        assert call.timeout == rest.DEFAULT_TIMEOUT

    def test_token_only_in_the_authorization_header(self):
        client, session, _ = _client(response(200, {}))
        client.request("GET", "/user")
        call = session.last
        assert TOKEN not in call.url
        assert TOKEN not in repr(call.params)
        assert TOKEN not in repr(client)

    def test_custom_accept_and_json_body(self):
        client, session, _ = _client(response(201, {"id": 1}))
        client.request("POST", "/repos/o/r/issues", json={"title": "t"}, accept="application/x")
        assert session.last.json == {"title": "t"}
        assert session.last.headers["Accept"] == "application/x"

    def test_owned_session_is_closed(self):
        with mock.patch.object(requests, "Session") as session_cls, GitHubRestClient(TOKEN):
            pass
        session_cls.return_value.close.assert_called_once()

    def test_injected_session_is_left_open(self):
        client, session, _ = _client()
        client.close()
        assert session.closed is False


class TestErrors:
    def test_non_2xx_raises_with_status_and_message(self):
        client, _, _ = _client(response(404, {"message": "Not Found"}))
        with pytest.raises(GitHubAPIError) as exc:
            client.request("GET", "/repos/o/r")
        assert exc.value.status == 404
        assert exc.value.message == "Not Found"
        assert str(exc.value) == "404: Not Found"
        assert exc.value.retry_after is None

    def test_validation_errors_are_appended(self):
        body = {
            "message": "Validation Failed",
            "errors": [
                {"resource": "Issue", "field": "title", "code": "missing_field"},
                {"message": "Reference already exists"},
                "plain text error",
            ],
        }
        assert error_message(response(422, body)) == (
            "Validation Failed (Issue title missing_field; Reference already exists; plain text error)"
        )

    def test_non_json_error_body_falls_back_to_reason(self):
        assert error_message(response(502, content=b"<html>bad gateway</html>")) == "Bad Gateway"

    def test_network_error_names_the_type_not_the_url(self):
        boom = requests.ConnectionError("HTTPSConnectionPool(host='api.github.com'): /search/code?q=secret-query")
        client, _, _ = _client(boom)
        with pytest.raises(GitHubAPIError) as exc:
            client.request("GET", "/search/code", params={"q": "secret-query"})
        assert exc.value.status == 0
        assert "ConnectionError" in str(exc.value)
        assert "secret-query" not in str(exc.value)
        assert exc.value.__cause__ is None and exc.value.__suppress_context__

    def test_logs_carry_no_token_path_or_arguments(self, caplog):
        caplog.set_level(logging.DEBUG, logger="aisquare.pipe.github")
        client, _, _ = _client(response(429, {"message": "slow down"}, headers={"Retry-After": "1"}), response(200, {}))
        client.request("GET", "/repos/acme/private-thing/contents/secret.txt", params={"q": "needle"})
        assert caplog.records, "expected debug/warning records"
        for needle in (TOKEN, "private-thing", "secret.txt", "needle"):
            assert needle not in caplog.text


class TestRateLimitRetry:
    def test_429_honours_retry_after_then_succeeds(self):
        client, session, sleeps = _client(
            response(429, {"message": "Too many"}, headers={"Retry-After": "3"}),
            response(200, {"ok": True}),
        )
        assert client.request("GET", "/user").json() == {"ok": True}
        assert sleeps == [3.0]
        assert len(session.calls) == 2

    def test_exhausted_primary_limit_waits_until_reset(self):
        client, _, sleeps = _client(
            response(
                403,
                {"message": "API rate limit exceeded for user ID 1."},
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(NOW) + 10)},
            ),
            response(200, {}),
        )
        client.request("GET", "/user")
        assert sleeps == [11.0]  # reset - now, plus a second of slack

    def test_secondary_limit_without_headers_waits_a_minute(self):
        client, _, sleeps = _client(
            response(403, {"message": "You have exceeded a secondary rate limit."}),
            response(200, {}),
        )
        client.request("GET", "/search/code")
        assert sleeps == [60.0]

    def test_retries_are_bounded(self):
        limited = [response(429, {"message": "Too many"}, headers={"Retry-After": "1"}) for _ in range(3)]
        client, session, sleeps = _client(*limited, max_retries=2)
        with pytest.raises(GitHubAPIError) as exc:
            client.request("GET", "/user")
        assert exc.value.status == 429
        assert exc.value.retry_after == 1.0
        assert exc.value.rate_limited
        assert sleeps == [1.0, 1.0]
        assert len(session.calls) == 3

    def test_wait_beyond_the_budget_fails_fast(self):
        client, session, sleeps = _client(
            response(
                403,
                {"message": "API rate limit exceeded"},
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(NOW) + 3600)},
            ),
            max_retry_wait=60,
        )
        with pytest.raises(GitHubAPIError) as exc:
            client.request("GET", "/user")
        assert sleeps == []
        assert len(session.calls) == 1
        assert exc.value.retry_after == 3601.0

    def test_total_sleep_stays_within_the_budget(self):
        limited = [response(429, {"message": "x"}, headers={"Retry-After": "40"}) for _ in range(2)]
        client, _, sleeps = _client(*limited, max_retries=5, max_retry_wait=60)
        with pytest.raises(GitHubAPIError):
            client.request("GET", "/user")
        assert sleeps == [40.0]

    def test_plain_403_is_not_retried(self):
        client, _, sleeps = _client(response(403, {"message": "Must have admin rights to Repository."}))
        with pytest.raises(GitHubAPIError) as exc:
            client.request("GET", "/repos/o/r/hooks")
        assert exc.value.status == 403
        assert exc.value.retry_after is None
        assert sleeps == []

    def test_server_error_on_get_is_retried(self):
        client, _, sleeps = _client(response(502, content=b""), response(200, {}))
        client.request("GET", "/user")
        assert sleeps == [1.0]

    def test_server_error_on_post_is_not_retried(self):
        client, session, sleeps = _client(response(502, content=b""))
        with pytest.raises(GitHubAPIError):
            client.request("POST", "/repos/o/r/issues", json={"title": "t"})
        assert sleeps == []
        assert len(session.calls) == 1

    @pytest.mark.parametrize(
        ("status", "headers", "body", "expected"),
        [
            (200, {}, {}, None),
            (404, {"Retry-After": "5"}, {"message": "Not Found"}, None),
            (429, {"Retry-After": "7"}, {}, 7.0),
            (429, {"Retry-After": "soon"}, {}, 60.0),
            (403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "junk"}, {}, 60.0),
            (403, {"x-ratelimit-remaining": "12"}, {"message": "Forbidden"}, None),
        ],
    )
    def test_rate_limit_wait(self, status, headers, body, expected):
        assert rate_limit_wait(response(status, body, headers=headers), now=NOW) == expected


class TestHelpers:
    def test_page_hint_reads_the_next_link(self):
        assert page_hint(response(200, [], headers=next_link(3))) == (True, 3)

    def test_page_hint_without_link(self):
        assert page_hint(response(200, [])) == (False, None)

    def test_page_hint_with_a_cursor_link(self):
        link = {"Link": '<https://api.github.com/x?after=Y3Vyc29y>; rel="next"'}
        assert page_hint(response(200, [], headers=link)) == (True, None)

    def test_json_body_of_204_is_none(self):
        assert json_body(response(204)) is None

    def test_json_body_rejects_non_json(self):
        with pytest.raises(GitHubAPIError):
            json_body(response(200, content=b"<html>"))


class TestRevokeGrant:
    def _revoke(self, *responses):
        session = FakeSession(list(responses))
        ok = revoke_grant(
            client_id="Iv1.abc", client_secret="app-secret", access_token=TOKEN, session=session  # type: ignore[arg-type]
        )
        return ok, session

    def test_deletes_the_grant_with_basic_auth(self):
        ok, session = self._revoke(response(204))
        assert ok is True
        call = session.last
        assert call.method == "DELETE"
        assert call.url == "https://api.github.com/applications/Iv1.abc/grant"
        assert call.auth == ("Iv1.abc", "app-secret")
        assert call.json == {"access_token": TOKEN}
        assert "Authorization" not in call.headers

    def test_non_204_is_false(self):
        ok, _ = self._revoke(response(404, {"message": "Not Found"}))
        assert ok is False

    def test_network_error_is_false_not_raised(self):
        ok, _ = self._revoke(requests.Timeout("timed out"))
        assert ok is False
