"""The Graph client boundary: where the token may go, how Graph's error
bodies and throttling headers are read, and session ownership."""

from __future__ import annotations

import pytest

from aisquare_pipe_onedrive.rest import (
    OneDriveAPIError,
    OneDriveRestClient,
    error_detail,
    json_body,
    throttle_wait,
)

from .fakes import TOKEN, UPLOAD_URL, FakeSession, graph_error, response


def _client(*answers, **kw):
    session = FakeSession(list(answers))
    return OneDriveRestClient(TOKEN, session=session, sleep=lambda s: None, **kw), session  # type: ignore[arg-type]


class TestWhereTheTokenGoes:
    def test_graph_paths_carry_the_bearer_token(self):
        client, session = _client(response(200, {}))
        client.get("/me/drive")
        assert session.last.url == "https://graph.microsoft.com/v1.0/me/drive"
        assert session.last.headers["Authorization"] == f"Bearer {TOKEN}"

    def test_absolute_urls_go_without_it(self):
        client, session = _client(response(202, {}))
        client.request("PUT", UPLOAD_URL, data=b"x", authenticated=False)
        assert session.last.url == UPLOAD_URL
        assert "Authorization" not in session.last.headers

    @pytest.mark.parametrize(
        ("url", "authenticated"),
        [(UPLOAD_URL, True), ("http://api.onedrive.com/x", False), ("graph.microsoft.com/v1.0/me", True)],
    )
    def test_never_sends_the_token_off_graph(self, url, authenticated):
        client, session = _client()
        with pytest.raises(ValueError):
            client.request("GET", url, authenticated=authenticated)
        assert session.calls == []

    def test_repr_has_no_token(self):
        client, _ = _client()
        assert TOKEN not in repr(client)


class TestErrorBodies:
    def test_graph_error_shape(self):
        assert error_detail(graph_error(404, "itemNotFound", "Item does not exist")) == ("itemNotFound", "itemNotFound: Item does not exist")

    def test_oauth_style_error(self):
        body = {"error": "invalid_token", "error_description": "token expired"}
        assert error_detail(response(401, body)) == ("invalid_token", "invalid_token: token expired")

    def test_non_json_falls_back_to_the_reason(self):
        assert error_detail(response(502, content=b"<html>bad gateway</html>", content_type="text/html")) == (None, "Bad Gateway")

    def test_long_messages_are_capped(self):
        assert len(error_detail(graph_error(400, "invalidRequest", "x" * 5000))[1]) == 500

    def test_raised_error_carries_status_and_code(self):
        client, _ = _client(graph_error(403, "accessDenied", "nope"))
        with pytest.raises(OneDriveAPIError) as exc:
            client.get("/me/drive")
        assert (exc.value.status, exc.value.code, exc.value.retry_after) == (403, "accessDenied", None)

    def test_empty_body_is_none(self):
        assert json_body(response(204)) is None


class TestThrottleWait:
    @pytest.mark.parametrize(
        ("status", "retry_after", "wait"),
        [
            (429, "7", 7.0),
            (429, None, 5.0),
            (429, "soon", 5.0),
            (509, None, 5.0),
            (503, "3", 3.0),
            (503, None, None),
            (500, "3", None),
            (404, None, None),
        ],
    )
    def test_wait(self, status, retry_after, wait):
        headers = {"Retry-After": retry_after} if retry_after else {}
        assert throttle_wait(graph_error(status, "x", "y", headers=headers)) == wait

    def test_server_errors_are_retried_on_get_only(self):
        client, session = _client(graph_error(503, "serviceNotAvailable", "x"), response(200, {}))
        client.get("/me/drive")
        assert len(session.calls) == 2
        client, session = _client(graph_error(503, "serviceNotAvailable", "x"))
        with pytest.raises(OneDriveAPIError):
            client.request("POST", "/me/drive/root/children", json={})
        assert len(session.calls) == 1


class TestSessionOwnership:
    def test_borrowed_session_is_not_closed(self):
        client, session = _client()
        with client:
            pass
        assert session.closed is False
