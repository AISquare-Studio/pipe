"""GitHub's OAuth App spec against core's flow, with GitHub's quirks: PKCE
(S256) on, a form-encoded token response, ``error`` inside a 200, no refresh
token, no expiry, and grant revocation via
``DELETE /applications/{client_id}/token`` (this token only)."""

from __future__ import annotations

import io
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message

import pytest
import requests
from aisquare.pipe import OAuthError, make_pkce_pair, pkce_challenge

from aisquare_pipe_github import GITHUB_OAUTH, GitHubToolkit

from .fakes import FakeSession, response

CLIENT_ID = "Ov23liExampleClientId"
VERIFIER, CHALLENGE = make_pkce_pair()
REDIRECT = "https://pipe.example/v1/connections/callback"
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


class FakeOpener:
    """Stands in for the no-redirect opener core builds per token request
    (``urllib.request.build_opener(_NoRedirect)``); records each Request."""

    def __init__(self, status: int, body: str, content_type: str, seen: list[urllib.request.Request]):
        self.status, self.body, self.content_type, self.seen = status, body, content_type, seen

    def open(self, request, timeout=None):
        self.seen.append(request)
        if self.status >= 400:
            raise urllib.error.HTTPError(request.full_url, self.status, "error", Message(), io.BytesIO(self.body.encode()))
        return FakeUrlResponse(self.status, self.body, self.content_type)


@pytest.fixture
def token_endpoint(monkeypatch):
    requests_seen: list[urllib.request.Request] = []

    def install(status: int, body: str, content_type: str = "application/x-www-form-urlencoded"):
        def build_opener(*handlers):
            return FakeOpener(status, body, content_type, requests_seen)

        monkeypatch.setattr(urllib.request, "build_opener", build_opener)
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

    def test_pkce_is_on(self):
        assert GITHUB_OAUTH.pkce is True

    def test_authorize_url_carries_an_s256_challenge(self):
        url = GitHubToolkit().build_authorize_url(
            client_id=CLIENT_ID, redirect_uri=REDIRECT, state="st4te", code_challenge=CHALLENGE
        )
        parts = urllib.parse.urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == GITHUB_OAUTH.authorize_url
        query = dict(urllib.parse.parse_qsl(parts.query))
        assert query == {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT,
            "state": "st4te",
            "scope": "repo read:user read:org",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",  # GitHub supports S256 only
        }
        # GitHub: "a 43 character SHA-256 hash of a random string".
        assert len(query["code_challenge"]) == 43
        assert pkce_challenge(VERIFIER) == CHALLENGE

    def test_authorize_url_without_a_challenge_is_refused(self):
        with pytest.raises(OAuthError) as exc:
            GitHubToolkit().build_authorize_url(client_id=CLIENT_ID, redirect_uri=REDIRECT, state="st4te")
        assert exc.value.error == "pkce_required"


class TestExchange:
    def test_form_encoded_token_response(self, token_endpoint):
        seen = token_endpoint(200, f"access_token={ACCESS}&scope=repo%2Cread%3Aorg%2Cread%3Auser&token_type=bearer")
        token = GitHubToolkit().exchange_code(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            code="code123",
            redirect_uri=REDIRECT,
            code_verifier=VERIFIER,
        )
        assert token.access_token == ACCESS
        assert token.refresh_token is None
        assert token.expires_at is None
        assert token.expired() is False
        assert token.scope == "repo,read:org,read:user"
        form = dict(urllib.parse.parse_qsl(seen[0].data.decode()))
        assert form["client_id"] == CLIENT_ID
        assert form["client_secret"] == CLIENT_SECRET  # GitHub still requires it with PKCE
        assert form["code_verifier"] == VERIFIER
        assert form["redirect_uri"] == REDIRECT
        assert seen[0].full_url == GITHUB_OAUTH.token_url

    def test_exchange_without_the_verifier_is_refused_before_any_request(self, token_endpoint):
        seen = token_endpoint(200, f"access_token={ACCESS}&token_type=bearer")
        with pytest.raises(OAuthError) as exc:
            GitHubToolkit().exchange_code(
                client_id=CLIENT_ID, client_secret=CLIENT_SECRET, code="code123", redirect_uri=REDIRECT
            )
        assert exc.value.error == "pkce_required"
        assert seen == []

    def test_verifier_mismatch_is_reported(self, token_endpoint):
        # What GitHub answers when a code is redeemed with another flow's verifier.
        token_endpoint(
            200,
            "error=invalid_grant&error_description=The+code_verifier+does+not+match+the+code_challenge",
        )
        other_flows_verifier, _ = make_pkce_pair()
        with pytest.raises(OAuthError) as exc:
            GitHubToolkit().exchange_code(
                client_id=CLIENT_ID,
                client_secret=CLIENT_SECRET,
                code="code123",
                redirect_uri=REDIRECT,
                code_verifier=other_flows_verifier,
            )
        assert exc.value.error == "invalid_grant"

    def test_error_inside_a_200(self, token_endpoint):
        token_endpoint(
            200,
            '{"error": "bad_verification_code", "error_description": "The code passed is incorrect or expired."}',
            "application/json",
        )
        with pytest.raises(OAuthError) as exc:
            GitHubToolkit().exchange_code(
                client_id=CLIENT_ID,
                client_secret=CLIENT_SECRET,
                code="stale",
                redirect_uri=REDIRECT,
                code_verifier=VERIFIER,
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
        assert (call.method, call.url) == ("DELETE", f"https://api.github.com/applications/{CLIENT_ID}/token")
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

    @pytest.mark.parametrize(
        ("client_id", "client_secret"),
        [(CLIENT_ID, "sécret-ünïcode-✓"), (None, CLIENT_SECRET)],
    )
    def test_never_raises_on_odd_credentials(self, client_id, client_secret):
        # A real Session: Basic auth encodes the secret as latin-1 before any
        # socket is opened, which raised UnicodeEncodeError before the fix.
        toolkit = GitHubToolkit(session=requests.Session())
        assert toolkit.revoke_token(client_id=client_id, client_secret=client_secret, token=ACCESS) is False
