"""Gmail's Google OAuth spec against core's flow: least privilege
(``gmail.readonly`` by default; write scopes on request), PKCE, offline
access with a forced consent (so every connect returns a refresh token), one-hour access
tokens, refresh without rotation, and RFC 7009-style revocation at
``oauth2.googleapis.com/revoke``. Core's token/revoke requests go through
``urllib.request.build_opener`` — patched here, so no socket is opened."""

from __future__ import annotations

import io
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message

import pytest
from aisquare.pipe import OAuthError, make_pkce_pair, pkce_challenge

from aisquare_pipe_gmail import GMAIL_OAUTH, GmailToolkit
from aisquare_pipe_gmail.tools import (
    COMPOSE_SCOPE,
    MODIFY_SCOPE,
    READONLY_SCOPE,
    SEND_SCOPE,
)

CLIENT_ID = "1234567890-abc.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-google-client-secret"
ACCESS = "ya29.a0AfB_newAccessToken0123456789"
REFRESH = "1//0gRefreshToken0123456789"
REDIRECT = "https://pipe.example/v1/connections/callback"


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
    """Stands in for the no-redirect opener core builds per request."""

    def __init__(self, status: int, body: str, content_type: str, seen: list[urllib.request.Request], error: Exception | None):
        self.status, self.body, self.content_type, self.seen, self.error = status, body, content_type, seen, error

    def open(self, request, timeout=None):
        self.seen.append(request)
        if self.error is not None:
            raise self.error
        if self.status >= 400:
            raise urllib.error.HTTPError(request.full_url, self.status, "error", Message(), io.BytesIO(self.body.encode()))
        return FakeUrlResponse(self.status, self.body, self.content_type)


@pytest.fixture
def endpoint(monkeypatch):
    seen: list[urllib.request.Request] = []

    def install(status: int = 200, body: str = "", content_type: str = "application/json", error: Exception | None = None):
        monkeypatch.setattr(
            urllib.request, "build_opener", lambda *handlers: FakeOpener(status, body, content_type, seen, error)
        )
        return seen

    return install


def _form(request: urllib.request.Request) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(request.data.decode()))


class TestSpec:
    def test_endpoints_scopes_and_flags(self):
        spec = GMAIL_OAUTH
        assert spec.provider == "google"
        assert spec.authorize_url == "https://accounts.google.com/o/oauth2/v2/auth"
        assert spec.token_url == "https://oauth2.googleapis.com/token"
        assert spec.revoke_url == "https://oauth2.googleapis.com/revoke"
        assert spec.default_scopes == (READONLY_SCOPE,) == ("https://www.googleapis.com/auth/gmail.readonly",)
        # No include_granted_scopes: a Gmail token carries Gmail scopes only,
        # even when the OAuth client also serves Drive and Calendar.
        assert dict(spec.extra_authorize_params) == {"access_type": "offline", "prompt": "consent"}
        assert (spec.pkce, spec.token_expires, spec.rotates_refresh_token) == (True, True, False)
        assert spec.token_auth_method == "client_secret_post"

    def test_toolkit_uses_the_spec(self):
        assert GmailToolkit.oauth is GMAIL_OAUTH

    def test_authorize_url(self):
        verifier, challenge = make_pkce_pair()
        url = GmailToolkit().build_authorize_url(
            client_id=CLIENT_ID, redirect_uri=REDIRECT, state="st4te", code_challenge=challenge
        )
        parts = urllib.parse.urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == GMAIL_OAUTH.authorize_url
        assert dict(urllib.parse.parse_qsl(parts.query)) == {
            "access_type": "offline",
            "prompt": "consent",
            "scope": READONLY_SCOPE,
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT,
            "state": "st4te",
            "code_challenge": pkce_challenge(verifier),
            "code_challenge_method": "S256",
        }

    def test_write_scopes_are_requested_on_top_of_the_default(self):
        _, challenge = make_pkce_pair()
        url = GmailToolkit().build_authorize_url(
            client_id=CLIENT_ID,
            redirect_uri=REDIRECT,
            state="s",
            scopes=[READONLY_SCOPE, SEND_SCOPE, COMPOSE_SCOPE, MODIFY_SCOPE],
            code_challenge=challenge,
        )
        assert dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))["scope"] == (
            "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.send "
            "https://www.googleapis.com/auth/gmail.compose https://www.googleapis.com/auth/gmail.modify"
        )

    def test_pkce_is_required(self):
        with pytest.raises(OAuthError) as exc:
            GmailToolkit().build_authorize_url(client_id=CLIENT_ID, redirect_uri=REDIRECT, state="s")
        assert exc.value.error == "pkce_required"


class TestExchange:
    def test_code_for_tokens(self, endpoint):
        seen = endpoint(
            200,
            f'{{"access_token": "{ACCESS}", "expires_in": 3599, "refresh_token": "{REFRESH}", '
            f'"scope": "{READONLY_SCOPE}", "token_type": "Bearer"}}',
        )
        before = time.time()
        token = GmailToolkit().exchange_code(
            client_id=CLIENT_ID, client_secret=CLIENT_SECRET, code="4/0Acode", redirect_uri=REDIRECT, code_verifier="v" * 64
        )
        assert (token.access_token, token.refresh_token, token.scope) == (ACCESS, REFRESH, READONLY_SCOPE)
        assert before + 3590 <= token.expires_at <= time.time() + 3600
        (request,) = seen
        assert request.full_url == GMAIL_OAUTH.token_url
        assert _form(request) == {
            "grant_type": "authorization_code",
            "code": "4/0Acode",
            "redirect_uri": REDIRECT,
            "code_verifier": "v" * 64,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        }

    def test_invalid_grant(self, endpoint):
        endpoint(400, '{"error": "invalid_grant", "error_description": "Bad Request"}')
        with pytest.raises(OAuthError) as exc:
            GmailToolkit().exchange_code(
                client_id=CLIENT_ID, client_secret=CLIENT_SECRET, code="stale", redirect_uri=REDIRECT, code_verifier="v" * 64
            )
        assert (exc.value.error, exc.value.status) == ("invalid_grant", 400)
        assert CLIENT_SECRET not in str(exc.value)


class TestRefresh:
    def test_refresh_keeps_the_refresh_token(self, endpoint):
        seen = endpoint(200, f'{{"access_token": "{ACCESS}", "expires_in": 3599, "token_type": "Bearer"}}')
        token = GmailToolkit().refresh_access_token(
            client_id=CLIENT_ID, client_secret=CLIENT_SECRET, refresh_token=REFRESH, previous_scope=READONLY_SCOPE
        )
        assert (token.access_token, token.refresh_token, token.scope) == (ACCESS, REFRESH, READONLY_SCOPE)
        assert _form(seen[0])["grant_type"] == "refresh_token"

    def test_revoked_refresh_token(self, endpoint):
        endpoint(400, '{"error": "invalid_grant", "error_description": "Token has been expired or revoked."}')
        with pytest.raises(OAuthError) as exc:
            GmailToolkit().refresh_access_token(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, refresh_token=REFRESH)
        assert exc.value.error == "invalid_grant"
        assert REFRESH not in str(exc.value)


class TestRevoke:
    def test_revokes_at_googles_endpoint(self, endpoint):
        seen = endpoint(200, "{}")
        assert GmailToolkit().revoke_token(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, token=REFRESH) is True
        (request,) = seen
        assert request.full_url == "https://oauth2.googleapis.com/revoke"
        assert _form(request)["token"] == REFRESH

    def test_failure_is_false(self, endpoint):
        endpoint(400, '{"error": "invalid_token"}')
        assert GmailToolkit().revoke_token(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, token=ACCESS) is False

    def test_network_error_is_false(self, endpoint):
        endpoint(error=urllib.error.URLError(OSError("unreachable")))
        assert GmailToolkit().revoke_token(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, token=ACCESS) is False

    @pytest.mark.parametrize(
        ("client_id", "client_secret", "token"),
        [(CLIENT_ID, "sécret-ünïcode-✓", ACCESS), (None, CLIENT_SECRET, ACCESS), (CLIENT_ID, None, None)],
    )
    def test_never_raises_on_odd_input(self, endpoint, client_id, client_secret, token):
        endpoint(200, "{}")
        result = GmailToolkit().revoke_token(client_id=client_id, client_secret=client_secret, token=token)  # type: ignore[arg-type]
        assert isinstance(result, bool)

    def test_unexpected_crash_is_false(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("bug")

        monkeypatch.setattr("aisquare_pipe_gmail.toolkit.oauth_revoke_token", boom)
        assert GmailToolkit().revoke_token(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, token=ACCESS) is False
