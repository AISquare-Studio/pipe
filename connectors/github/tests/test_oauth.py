"""GitHub's OAuth App spec against core's flow, with GitHub's quirks: a
form-encoded token response, ``error`` inside a 200, no refresh token, no
expiry, and grant revocation via ``DELETE /applications/{client_id}/grant``."""

from __future__ import annotations

import io
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message

import pytest
import requests
from aisquare.pipe import OAuthError

from aisquare_pipe_github import GITHUB_OAUTH, GitHubToolkit

from .fakes import FakeSession, response

CLIENT_ID = "Ov23liExampleClientId"
CLIENT_SECRET = "github-app-secret-value"
ACCESS = "gho_newAccessToken0123456789"


class FakeUrlResponse:
    def __init__(self, status: int, body: str, content_type: str):
        self.status = status
        self.body = body.encode()
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def read(self) -> bytes:
        return self.body

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def token_endpoint(monkeypatch):
    requests_seen: list[urllib.request.Request] = []

    def install(status: int, body: str, content_type: str = "application/x-www-form-urlencoded"):
        def urlopen(request, timeout=None):
            requests_seen.append(request)
            if status >= 400:
                raise urllib.error.HTTPError(request.full_url, status, "error", Message(), io.BytesIO(body.encode()))
            return FakeUrlResponse(status, body, content_type)

        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
        return requests_seen

    return install


class TestSpec:
    def test_endpoints_scopes_and_expiry(self):
        assert GITHUB_OAUTH.provider == "github"
        assert GITHUB_OAUTH.authorize_url == "https://github.com/login/oauth/authorize"
        assert GITHUB_OAUTH.token_url == "https://github.com/login/oauth/access_token"
        assert GITHUB_OAUTH.default_scopes == ("repo", "read:user", "read:org")
        assert GITHUB_OAUTH.token_expires is False
        assert GITHUB_OAUTH.revoke_url is None  # not RFC 7009; revoke_token is overridden

    def test_authorize_url(self):
        url = GitHubToolkit().build_authorize_url(
            client_id=CLIENT_ID, redirect_uri="https://pipe.example/v1/connections/callback", state="st4te"
        )
        parts = urllib.parse.urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == GITHUB_OAUTH.authorize_url
        query = dict(urllib.parse.parse_qsl(parts.query))
        assert query == {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": "https://pipe.example/v1/connections/callback",
            "state": "st4te",
            "scope": "repo read:user read:org",
        }


class TestExchange:
    def test_form_encoded_token_response(self, token_endpoint):
        seen = token_endpoint(200, f"access_token={ACCESS}&scope=repo%2Cread%3Aorg%2Cread%3Auser&token_type=bearer")
        token = GitHubToolkit().exchange_code(
            client_id=CLIENT_ID, client_secret=CLIENT_SECRET, code="code123", redirect_uri="https://pipe.example/cb"
        )
        assert token.access_token == ACCESS
        assert token.refresh_token is None
        assert token.expires_at is None
        assert token.expired() is False
        assert token.scope == "repo,read:org,read:user"
        form = dict(urllib.parse.parse_qsl(seen[0].data.decode()))
        assert form["client_id"] == CLIENT_ID
        assert form["client_secret"] == CLIENT_SECRET
        assert seen[0].full_url == GITHUB_OAUTH.token_url

    def test_error_inside_a_200(self, token_endpoint):
        token_endpoint(
            200,
            '{"error": "bad_verification_code", "error_description": "The code passed is incorrect or expired."}',
            "application/json",
        )
        with pytest.raises(OAuthError) as exc:
            GitHubToolkit().exchange_code(
                client_id=CLIENT_ID, client_secret=CLIENT_SECRET, code="stale", redirect_uri="https://pipe.example/cb"
            )
        assert exc.value.error == "bad_verification_code"
        assert CLIENT_SECRET not in str(exc.value)


class TestRevoke:
    def _toolkit(self, *responses):
        session = FakeSession(list(responses))
        return GitHubToolkit(session=session), session  # type: ignore[arg-type]

    def test_revokes_the_grant(self):
        toolkit, session = self._toolkit(response(204))
        assert toolkit.revoke_token(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, token=ACCESS) is True
        call = session.last
        assert (call.method, call.url) == ("DELETE", f"https://api.github.com/applications/{CLIENT_ID}/grant")
        assert call.auth == (CLIENT_ID, CLIENT_SECRET)
        assert call.json == {"access_token": ACCESS}

    def test_without_a_client_secret_nothing_is_sent(self):
        toolkit, session = self._toolkit()
        assert toolkit.revoke_token(client_id=CLIENT_ID, client_secret=None, token=ACCESS) is False
        assert session.calls == []

    def test_failure_is_false(self):
        toolkit, _ = self._toolkit(response(422, {"message": "Validation Failed"}))
        assert toolkit.revoke_token(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, token=ACCESS) is False

    def test_network_error_is_false(self):
        toolkit, _ = self._toolkit(requests.ConnectionError("down"))
        assert toolkit.revoke_token(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, token=ACCESS) is False
