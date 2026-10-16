"""Tests for the OAuth flow primitives (core/oauth.py) — urllib is mocked,
nothing touches the network."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message

import pytest

from aisquare.pipe import (
    OAuthError,
    OAuthSpec,
    OAuthToken,
    PipeError,
    build_authorize_url,
    exchange_code,
    make_pkce_pair,
    refresh_access_token,
    revoke_token,
)
from aisquare.pipe.testing.mock_connectors import MockToolkit

SPEC = OAuthSpec(
    provider="acme",
    authorize_url="https://acme.example/oauth/authorize",
    token_url="https://acme.example/oauth/token",
    revoke_url="https://acme.example/oauth/revoke",
    default_scopes=("read", "write"),
)
GITHUB_LIKE = OAuthSpec(
    provider="github",
    authorize_url="https://github.com/login/oauth/authorize",
    token_url="https://github.com/login/oauth/access_token",
    default_scopes=("repo", "read:user"),
    scope_separator=",",
    token_expires=False,
)
SECRETS = ("client-secret-xyz", "auth-code-abc", "refresh-old-123", "access-new-456")


class FakeResponse:
    def __init__(self, status: int, body: bytes | str | dict, content_type: str = "application/json"):
        if isinstance(body, dict):
            body = json.dumps(body)
        self.status = status
        self.body = body.encode() if isinstance(body, str) else body
        self.headers = Message()
        if content_type:
            self.headers["Content-Type"] = content_type

    def read(self) -> bytes:
        return self.body

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeUrlopen:
    """Stands in for urllib.request.urlopen; records every Request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if response.status >= 400:
            raise urllib.error.HTTPError(
                request.full_url, response.status, "error", response.headers, io.BytesIO(response.body)
            )
        return response

    @property
    def form(self) -> dict[str, str]:
        return dict(urllib.parse.parse_qsl(self.requests[-1].data.decode()))


@pytest.fixture
def http(monkeypatch):
    def install(*responses) -> FakeUrlopen:
        fake = FakeUrlopen(*responses)
        monkeypatch.setattr(urllib.request, "urlopen", fake)
        return fake

    return install


def _assert_no_secrets(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text


# --------------------------------------------------------------------------
# PKCE + authorize URL
# --------------------------------------------------------------------------


class TestPkce:
    def test_pair_is_s256(self):
        verifier, challenge = make_pkce_pair()
        assert 43 <= len(verifier) <= 128
        assert re.fullmatch(r"[A-Za-z0-9\-._~]+", verifier)
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
        assert challenge == expected.decode()
        assert "=" not in challenge

    def test_pairs_are_unique(self):
        assert make_pkce_pair() != make_pkce_pair()


def _query(url: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


class TestBuildAuthorizeUrl:
    def test_standard_params_and_default_scopes(self):
        url = build_authorize_url(SPEC, client_id="cid", redirect_uri="https://app/cb", state="st8")
        assert url.startswith("https://acme.example/oauth/authorize?")
        assert _query(url) == {
            "response_type": "code",
            "client_id": "cid",
            "redirect_uri": "https://app/cb",
            "state": "st8",
            "scope": "read write",
        }

    def test_scope_separator(self):
        url = build_authorize_url(GITHUB_LIKE, client_id="c", redirect_uri="https://r", state="s")
        assert _query(url)["scope"] == "repo,read:user"

    def test_explicit_scopes_override_defaults(self):
        url = build_authorize_url(SPEC, client_id="c", redirect_uri="https://r", state="s", scopes=["admin"])
        assert _query(url)["scope"] == "admin"

    def test_empty_scopes_omit_param(self):
        url = build_authorize_url(SPEC, client_id="c", redirect_uri="https://r", state="s", scopes=[])
        assert "scope" not in _query(url)

    def test_extra_params_and_existing_query(self):
        spec = OAuthSpec(
            provider="google",
            authorize_url="https://accounts.example/o/oauth2/auth?hd=example.com&state=baked",
            token_url="https://accounts.example/token",
            extra_authorize_params={"access_type": "offline", "prompt": "consent"},
        )
        query = _query(build_authorize_url(spec, client_id="c", redirect_uri="https://r", state="mine"))
        assert query["access_type"] == "offline"
        assert query["prompt"] == "consent"
        assert query["hd"] == "example.com"
        assert query["state"] == "mine"  # the flow's value wins over a baked-in one

    def test_pkce_challenge(self):
        _, challenge = make_pkce_pair()
        url = build_authorize_url(
            SPEC, client_id="c", redirect_uri="https://r", state="s", code_challenge=challenge
        )
        assert _query(url)["code_challenge"] == challenge
        assert _query(url)["code_challenge_method"] == "S256"

    def test_pkce_spec_requires_challenge(self):
        spec = OAuthSpec(provider="p", authorize_url="https://a", token_url="https://t", pkce=True)
        with pytest.raises(OAuthError) as info:
            build_authorize_url(spec, client_id="c", redirect_uri="https://r", state="s")
        assert info.value.error == "pkce_required"


# --------------------------------------------------------------------------
# Code exchange
# --------------------------------------------------------------------------


class TestExchangeCode:
    def test_json_response(self, http, monkeypatch):
        monkeypatch.setattr("time.time", lambda: 1_000_000.0)
        fake = http(
            FakeResponse(
                200,
                {
                    "access_token": "access-new-456",
                    "refresh_token": "refresh-new-789",
                    "expires_in": 3600,
                    "scope": "read write",
                    "token_type": "bearer",
                },
            )
        )
        token = exchange_code(
            SPEC,
            client_id="cid",
            client_secret="client-secret-xyz",
            code="auth-code-abc",
            redirect_uri="https://app/cb",
            code_verifier="verifier-1",
        )
        assert token.to_dict() == {
            "access_token": "access-new-456",
            "refresh_token": "refresh-new-789",
            "expires_at": 1_003_600,
            "scope": "read write",
            "token_type": "bearer",
        }
        request = fake.requests[0]
        assert request.full_url == SPEC.token_url
        assert request.get_method() == "POST"
        assert request.get_header("Accept") == "application/json"
        assert request.get_header("Content-type") == "application/x-www-form-urlencoded"
        assert fake.form == {
            "grant_type": "authorization_code",
            "code": "auth-code-abc",
            "redirect_uri": "https://app/cb",
            "code_verifier": "verifier-1",
            "client_id": "cid",
            "client_secret": "client-secret-xyz",
        }

    def test_form_encoded_response(self, http):
        # GitHub answers form-encoded (and without expiry for OAuth apps).
        http(
            FakeResponse(
                200,
                "access_token=gho_abc&scope=repo%2Cread%3Auser&token_type=bearer",
                content_type="application/x-www-form-urlencoded; charset=utf-8",
            )
        )
        token = exchange_code(
            GITHUB_LIKE, client_id="c", client_secret="s", code="x", redirect_uri="https://r"
        )
        assert token.access_token == "gho_abc"
        assert token.scope == "repo,read:user"
        assert token.refresh_token is None
        assert token.expires_at is None and not token.expired()

    def test_form_body_without_content_type(self, http):
        http(FakeResponse(200, "access_token=tok&token_type=bearer", content_type=""))
        token = exchange_code(SPEC, client_id="c", client_secret="s", code="x", redirect_uri="https://r")
        assert token.access_token == "tok"

    def test_no_verifier_param_when_absent(self, http):
        fake = http(FakeResponse(200, {"access_token": "t"}))
        exchange_code(SPEC, client_id="c", client_secret="s", code="x", redirect_uri="https://r")
        assert "code_verifier" not in fake.form

    def test_client_secret_basic(self, http):
        spec = OAuthSpec(
            provider="basic",
            authorize_url="https://a",
            token_url="https://t.example/token",
            token_auth_method="client_secret_basic",
        )
        fake = http(FakeResponse(200, {"access_token": "t"}))
        exchange_code(spec, client_id="cid", client_secret="client-secret-xyz", code="x", redirect_uri="https://r")
        expected = base64.b64encode(b"cid:client-secret-xyz").decode()
        assert fake.requests[0].get_header("Authorization") == f"Basic {expected}"
        assert "client_secret" not in fake.form and "client_id" not in fake.form

    def test_public_client_sends_no_secret(self, http):
        fake = http(FakeResponse(200, {"access_token": "t"}))
        exchange_code(SPEC, client_id="cid", client_secret=None, code="x", redirect_uri="https://r")
        assert fake.form["client_id"] == "cid" and "client_secret" not in fake.form


# --------------------------------------------------------------------------
# Refresh
# --------------------------------------------------------------------------


class TestRefresh:
    def test_keeps_old_refresh_token_when_none_returned(self, http):
        fake = http(FakeResponse(200, {"access_token": "access-new-456", "expires_in": 3599}))
        token = refresh_access_token(
            SPEC, client_id="cid", client_secret="client-secret-xyz", refresh_token="refresh-old-123"
        )
        assert token.access_token == "access-new-456"
        assert token.refresh_token == "refresh-old-123"
        assert fake.form == {
            "grant_type": "refresh_token",
            "refresh_token": "refresh-old-123",
            "client_id": "cid",
            "client_secret": "client-secret-xyz",
        }

    def test_rotation_takes_new_refresh_token(self, http):
        http(FakeResponse(200, {"access_token": "a2", "refresh_token": "refresh-rotated"}))
        token = refresh_access_token(SPEC, client_id="c", client_secret="s", refresh_token="refresh-old-123")
        assert token.refresh_token == "refresh-rotated"

    def test_scopes_param(self, http):
        fake = http(FakeResponse(200, {"access_token": "a"}))
        refresh_access_token(
            SPEC, client_id="c", client_secret="s", refresh_token="r", scopes=["Files.Read", "offline_access"]
        )
        assert fake.form["scope"] == "Files.Read offline_access"

    def test_form_encoded_refresh(self, http):
        http(
            FakeResponse(
                200,
                "access_token=ghu_new&expires_in=28800&refresh_token=ghr_new&token_type=bearer",
                content_type="application/x-www-form-urlencoded",
            )
        )
        token = refresh_access_token(GITHUB_LIKE, client_id="c", client_secret="s", refresh_token="ghr_old")
        assert (token.access_token, token.refresh_token) == ("ghu_new", "ghr_new")
        assert token.expires_at is not None


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class TestOAuthErrors:
    def test_http_400_json_error(self, http):
        http(FakeResponse(400, {"error": "invalid_grant", "error_description": "Bad refresh token."}))
        with pytest.raises(OAuthError) as info:
            refresh_access_token(
                SPEC, client_id="c", client_secret="client-secret-xyz", refresh_token="refresh-old-123"
            )
        err = info.value
        assert isinstance(err, PipeError)
        assert (err.provider, err.error, err.status) == ("acme", "invalid_grant", 400)
        assert err.description == "Bad refresh token."
        assert str(err) == "acme: token refresh failed (invalid_grant): Bad refresh token."
        _assert_no_secrets(str(err))

    def test_error_in_200_form_body(self, http):
        # GitHub reports a bad code as HTTP 200 with an error field.
        http(
            FakeResponse(
                200,
                "error=bad_verification_code&error_description=The+code+passed+is+incorrect+or+expired.",
                content_type="application/x-www-form-urlencoded",
            )
        )
        with pytest.raises(OAuthError) as info:
            exchange_code(
                GITHUB_LIKE,
                client_id="c",
                client_secret="client-secret-xyz",
                code="auth-code-abc",
                redirect_uri="https://r",
            )
        assert info.value.error == "bad_verification_code"
        assert info.value.status == 200
        _assert_no_secrets(str(info.value))

    def test_http_error_without_body(self, http):
        http(FakeResponse(503, b"", content_type="text/html"))
        with pytest.raises(OAuthError) as info:
            exchange_code(SPEC, client_id="c", client_secret="s", code="x", redirect_uri="https://r")
        assert info.value.error == "http_503"

    def test_html_error_page(self, http):
        http(FakeResponse(500, "<html>oops</html>", content_type="text/html"))
        with pytest.raises(OAuthError) as info:
            exchange_code(SPEC, client_id="c", client_secret="s", code="x", redirect_uri="https://r")
        assert info.value.error == "http_500"

    def test_network_failure(self, http):
        http(urllib.error.URLError("Name or service not known"))
        with pytest.raises(OAuthError) as info:
            exchange_code(
                SPEC, client_id="c", client_secret="client-secret-xyz", code="auth-code-abc", redirect_uri="https://r"
            )
        assert info.value.error == "network_error"
        assert info.value.__cause__ is None and info.value.__suppress_context__
        _assert_no_secrets(str(info.value))

    def test_missing_access_token(self, http):
        http(FakeResponse(200, {"token_type": "bearer"}))
        with pytest.raises(OAuthError) as info:
            exchange_code(SPEC, client_id="c", client_secret="s", code="x", redirect_uri="https://r")
        assert info.value.error == "invalid_token_response"


# --------------------------------------------------------------------------
# Revoke
# --------------------------------------------------------------------------


class TestRevoke:
    def test_success(self, http):
        fake = http(FakeResponse(200, b""))
        assert revoke_token(SPEC, client_id="c", client_secret="s", token="access-new-456") is True
        assert fake.requests[0].full_url == SPEC.revoke_url
        assert fake.form == {
            "token": "access-new-456",
            "token_type_hint": "access_token",
            "client_id": "c",
            "client_secret": "s",
        }

    def test_failure_status_is_false(self, http):
        http(FakeResponse(400, {"error": "unsupported_token_type"}))
        assert revoke_token(SPEC, client_id="c", client_secret="s", token="t") is False

    def test_network_failure_is_false(self, http):
        http(urllib.error.URLError("down"))
        assert revoke_token(SPEC, client_id="c", client_secret="s", token="t") is False

    def test_no_revoke_url_makes_no_request(self, http):
        fake = http()
        assert revoke_token(GITHUB_LIKE, client_id="c", client_secret="s", token="t") is False
        assert fake.requests == []


# --------------------------------------------------------------------------
# OAuthToken + Toolkit delegation
# --------------------------------------------------------------------------


class TestOAuthToken:
    def test_expired_with_skew(self):
        token = OAuthToken(access_token="a", expires_at=1000)
        assert not token.expired(now=900)
        assert token.expired(now=950)  # inside the default 60s skew
        assert not token.expired(skew=0, now=999)
        assert token.expired(skew=0, now=1000)

    def test_no_expiry_never_expires(self):
        assert not OAuthToken(access_token="a").expired(now=10**12)

    def test_round_trip(self):
        token = OAuthToken("a", refresh_token="r", expires_at=5, scope="x y", token_type="bearer")
        assert OAuthToken.from_dict(token.to_dict()) == token
        assert OAuthToken.from_dict({"access_token": "a"}) == OAuthToken(access_token="a")

    def test_repr_hides_secrets(self):
        text = repr(OAuthToken("access-new-456", refresh_token="refresh-old-123", expires_at=5))
        _assert_no_secrets(text)
        assert "expires_at=5" in text

    def test_from_response_scope_list_and_bad_expiry(self):
        token = OAuthToken.from_response({"access_token": "a", "scope": ["a", "b"], "expires_in": "soon"})
        assert token.scope == "a b" and token.expires_at is None


class TestToolkitDelegation:
    def test_exchange_and_refresh_via_toolkit(self, http):
        fake = http(
            FakeResponse(200, {"access_token": "a1", "refresh_token": "r1", "expires_in": 60}),
            FakeResponse(200, {"access_token": "a2", "refresh_token": "r2"}),
        )
        kit = MockToolkit()
        first = kit.exchange_code(
            client_id="c", client_secret="s", code="x", redirect_uri="https://r", code_verifier="v"
        )
        second = kit.refresh_access_token(client_id="c", client_secret="s", refresh_token=first.refresh_token or "")
        assert (first.access_token, second.access_token, second.refresh_token) == ("a1", "a2", "r2")
        assert all(r.full_url == kit.oauth.token_url for r in fake.requests)  # type: ignore[union-attr]

    def test_revoke_via_toolkit(self, http):
        http(FakeResponse(200, b""))
        assert MockToolkit().revoke_token(client_id="c", client_secret="s", token="t") is True
