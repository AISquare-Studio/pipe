"""SlackWebClient — encoding, the token's place, Slack's ok:false folding,
retries, Retry-After parsing and the time budget.

HTTP is a FakeSession (see tests/fakes.py); sleeping and the clock are
injected, so retry waits are asserted without waiting.
"""

from __future__ import annotations

import threading
from unittest import mock

import pytest
import requests

from aisquare_pipe_slack import SlackToolkit
from aisquare_pipe_slack.rest import (
    DEFAULT_RETRY_AFTER,
    SlackAPIError,
    SlackWebClient,
    form_value,
    next_cursor,
    retry_after_seconds,
)

from .fakes import USER_TOKEN, FakeSession, ok, response, slack_error


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _client(*responses, **kwargs) -> tuple[SlackWebClient, FakeSession, list[float]]:
    session = FakeSession(list(responses))
    sleeps: list[float] = []
    client = SlackWebClient(USER_TOKEN, session=session, sleep=sleeps.append, **kwargs)  # type: ignore[arg-type]
    return client, session, sleeps


def _raises(client: SlackWebClient, method: str = "auth.test", **kwargs) -> SlackAPIError:
    with pytest.raises(SlackAPIError) as exc:
        client.call(method, **kwargs)
    return exc.value


class TestRequest:
    def test_form_encoding(self):
        client, session, _ = _client(ok())
        client.call(
            "conversations.list",
            {"types": "im", "exclude_archived": True, "limit": 200.0, "cursor": None, "x": [1], "y": {"a": 1}},
        )
        call = session.last
        assert call.url == "https://slack.com/api/conversations.list"
        assert call.data == {
            "types": "im",
            "exclude_archived": "true",
            "limit": "200",
            "x": "[1]",
            "y": '{"a": 1}',
        }
        assert "Content-Type" not in call.headers

    def test_json_encoding(self):
        client, session, _ = _client(ok())
        client.call("chat.postMessage", {"channel": "C1", "blocks": [{"type": "divider"}], "x": None}, json_body=True)
        call = session.last
        assert call.headers["Content-Type"] == "application/json; charset=utf-8"
        assert call.args == {"channel": "C1", "blocks": [{"type": "divider"}]}

    def test_token_only_in_the_authorization_header(self):
        client, session, _ = _client(ok(), ok())
        client.call("auth.test")
        client.call("chat.postMessage", {"channel": "C1"}, json_body=True)
        for call in session.calls:
            assert call.headers["Authorization"] == f"Bearer {USER_TOKEN}"
            assert USER_TOKEN not in call.url
            assert USER_TOKEN not in repr(call.data)
        assert USER_TOKEN not in repr(client)

    def test_redirects_are_not_followed(self):
        client, session, _ = _client(response(302, headers={"Location": "https://evil.test/"}))
        error = _raises(client)
        assert (error.status, error.error) == (302, "http_302")
        assert session.last.allow_redirects is False

    @pytest.mark.parametrize(
        "method",
        ["../oauth.v2.access", "auth.test?x=1", "https://evil.test/a.b", "auth", "SlackLists.create", "a.b/c",
         "auth.test\n", "auth.test\r\n"],
    )
    def test_method_names_are_checked(self, method):
        client, session, _ = _client()
        with pytest.raises(ValueError):
            client.call(method)
        assert session.calls == []

    @pytest.mark.parametrize("method", ["slackLists.items.list", "files.remote.add", "users.profile.get"])
    def test_camel_case_method_names_are_allowed(self, method):
        client, session, _ = _client(ok())
        client.call(method)
        assert session.last.url == f"https://slack.com/api/{method}"

    def test_owned_session_is_closed(self):
        with mock.patch.object(requests, "Session") as session_cls, SlackWebClient(USER_TOKEN):
            pass
        session_cls.return_value.close.assert_called_once()

    def test_injected_session_is_left_open(self):
        client, session, _ = _client()
        client.close()
        assert session.closed is False

    def test_each_call_gets_its_own_session(self):
        sessions: list[FakeSession] = []
        lock = threading.Lock()

        def new_session() -> FakeSession:
            session = FakeSession([ok(user_id="U1")])
            with lock:
                sessions.append(session)
            return session

        toolkit = SlackToolkit()
        results = []
        with mock.patch.object(requests, "Session", side_effect=new_session):
            threads = [
                threading.Thread(
                    target=lambda: results.append(toolkit.call_tool("SLACK_TEST_AUTH", {}, {"access_token": "t"}))
                )
                for _ in range(16)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        assert len(sessions) == 16
        assert all(s.closed and len(s.calls) == 1 for s in sessions)
        assert all(r.successful for r in results)


class TestOutcome:
    def test_ok_payload_is_returned(self):
        client, _, _ = _client(ok(user_id="U1", warning="superfluous_charset"))
        assert client.call("auth.test")["user_id"] == "U1"

    def test_ok_false_raises_with_the_code(self):
        error = _raises(_client(slack_error("channel_not_found"))[0])
        assert (error.method, error.error, error.status) == ("auth.test", "channel_not_found", 200)

    def test_missing_scope_keeps_needed(self):
        error = _raises(_client(slack_error("missing_scope", needed="chat:write", provided="users:read"))[0])
        assert (error.error, error.needed) == ("missing_scope", "chat:write")

    @pytest.mark.parametrize(
        "reply, code",
        [
            (response(200, content=b"<html>oops</html>"), "invalid_response"),
            (response(200, [1, 2]), "invalid_response"),
            (response(200, content=b""), "invalid_response"),
            (response(200, content=b"[" * 100_000), "invalid_response"),
            (response(200, {"ok": False, "error": 7}), "unknown_error"),
            (response(200, {"ok": False, "error": {"x": 1}}), "unknown_error"),
            (response(200, {"ok": "true"}), "unknown_error"),
            (response(200, {"error": "invalid_auth"}), "invalid_auth"),
            (response(400, content=b"bad"), "http_400"),
            (response(404, {"ok": False, "error": "unknown_method"}), "unknown_method"),
            (response(500, content=b"down"), "http_500"),
        ],
    )
    def test_odd_bodies_keep_their_status(self, reply, code):
        error = _raises(_client(reply)[0])
        assert (error.error, error.status) == (code, reply.status_code)

    def test_timeout(self):
        error = _raises(_client(requests.ReadTimeout("slow https://slack.com/api/x?token=t"))[0])
        assert (error.error, error.timed_out, error.status) == ("timeout", True, 0)

    def test_network_error_names_no_url(self):
        error = _raises(_client(requests.ConnectionError(f"https://slack.com/api/x {USER_TOKEN}"))[0])
        assert (error.error, error.status) == ("network_error", 0)
        assert USER_TOKEN not in str(error)


class TestRetries:
    def test_429_honours_retry_after_then_succeeds(self):
        client, session, sleeps = _client(
            response(429, {"ok": False, "error": "ratelimited"}, headers={"Retry-After": "3"}), ok(x=1)
        )
        assert client.call("chat.postMessage", {"channel": "C1"}, json_body=True)["x"] == 1
        assert sleeps == [3.0]
        assert len(session.calls) == 2

    def test_ok_false_ratelimited_is_retried_too(self):
        client, _, sleeps = _client(slack_error("ratelimited"), ok())
        client.call("auth.test")
        assert sleeps == [DEFAULT_RETRY_AFTER]

    def test_retries_are_bounded(self):
        limited = [response(429, headers={"Retry-After": "1"}) for _ in range(3)]
        client, session, sleeps = _client(*limited)
        error = _raises(client)
        assert (error.status, error.retry_after) == (429, 1.0)
        assert sleeps == [1.0, 1.0]
        assert len(session.calls) == 3

    def test_total_sleep_stays_within_max_retry_wait(self):
        limited = [response(429, headers={"Retry-After": "40"}) for _ in range(3)]
        client, _, sleeps = _client(*limited, max_retries=5, max_retry_wait=60)
        assert _raises(client).retry_after == 40.0
        assert sleeps == [40.0]

    @pytest.mark.parametrize("retry_after", ["5", "4.9", "3.01"])
    def test_floor_rule_fails_fast_instead_of_sleeping_the_budget_away(self, retry_after):
        clock = FakeClock()
        client, session, sleeps = _client(
            response(429, headers={"Retry-After": retry_after}), ok(), budget=5, monotonic=clock
        )
        error = _raises(client)
        assert error.retry_after == float(retry_after)
        assert (sleeps, len(session.calls)) == ([], 1)

    def test_a_wait_that_fits_the_budget_is_slept(self):
        clock = FakeClock()
        client, _, sleeps = _client(response(429, headers={"Retry-After": "2"}), ok(), budget=5, monotonic=clock)
        client.call("auth.test")
        assert sleeps == [2.0]

    def test_an_exhausted_budget_is_a_timeout(self):
        clock = FakeClock()
        client, session, _ = _client(budget=5, monotonic=clock)
        clock.now += 6
        error = _raises(client)
        assert error.timed_out and session.calls == []

    def test_request_timeout_shrinks_to_the_budget(self):
        clock = FakeClock()
        client, session, _ = _client(ok(), budget=5, monotonic=clock)
        clock.now += 2
        client.call("auth.test")
        assert session.last.timeout == pytest.approx(3.0)

    @pytest.mark.parametrize("reply", [response(503, content=b"x"), slack_error("internal_error")])
    def test_server_errors_are_retried_for_reads(self, reply):
        client, session, sleeps = _client(reply, ok())
        client.call("conversations.history", {"channel": "C1"}, idempotent=True)
        assert sleeps == [1.0]
        assert len(session.calls) == 2

    @pytest.mark.parametrize("reply", [response(503, content=b"x"), slack_error("service_unavailable")])
    def test_server_errors_are_not_retried_for_writes(self, reply):
        client, session, sleeps = _client(reply, ok())
        _raises(client, "chat.postMessage")
        assert (sleeps, len(session.calls)) == ([], 1)

    def test_other_errors_are_not_retried(self):
        client, session, sleeps = _client(slack_error("channel_not_found"), ok())
        _raises(client, "conversations.history", idempotent=True)
        assert (sleeps, len(session.calls)) == ([], 1)


class TestHelpers:
    @pytest.mark.parametrize(
        "value, expected",
        [
            ("30", 30.0),
            ("1.5", 1.5),
            ("0", 0.0),
            ("99999", 3600.0),
            (None, DEFAULT_RETRY_AFTER),
            ("inf", DEFAULT_RETRY_AFTER),
            ("1e309", DEFAULT_RETRY_AFTER),
            ("nan", DEFAULT_RETRY_AFTER),
            ("-5", DEFAULT_RETRY_AFTER),
            ("soon", DEFAULT_RETRY_AFTER),
            ("Wed, 21 Oct 2026 07:28:00 GMT", DEFAULT_RETRY_AFTER),
        ],
    )
    def test_retry_after_seconds(self, value, expected):
        headers = {"Retry-After": value} if value is not None else {}
        assert retry_after_seconds(response(429, headers=headers)) == expected

    @pytest.mark.parametrize(
        "value, expected", [(True, "true"), (False, "false"), (3, "3"), (2.0, "2"), (2.5, "2.5"), ("x", "x")]
    )
    def test_form_value(self, value, expected):
        assert form_value(value) == expected

    @pytest.mark.parametrize(
        "payload, expected",
        [
            ({"response_metadata": {"next_cursor": "abc"}}, "abc"),
            ({"response_metadata": {"next_cursor": ""}}, None),
            ({"response_metadata": {"next_cursor": None}}, None),
            ({"response_metadata": "x"}, None),
            ({}, None),
        ],
    )
    def test_next_cursor(self, payload, expected):
        assert next_cursor(payload) == expected
