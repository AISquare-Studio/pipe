"""Tests for the OAuth flow primitives (core/oauth.py) — urllib is mocked,
nothing touches the network."""

from __future__ import annotations

import base64
import hashlib
import http.client as http_client
import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message

import pytest

from aisquare.pipe import (
    OAuthAccount,
    OAuthError,
    OAuthSpec,
    OAuthToken,
    PipeError,
    build_authorize_url,
    exchange_code,
    make_pkce_pair,
    pkce_challenge,
    refresh_access_token,
    revoke_token,
)
from aisquare.pipe.core.oauth import FLOW_AUTHORIZE_PARAMS, account_from_id_token
from aisquare.pipe.testing.mock_connectors import MOCK_OAUTH, MockToolkit

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

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Stands in for the opener urllib.request.build_opener returns; records
    every Request and the handlers the opener was built with."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[urllib.request.Request] = []
        self.handlers: tuple = ()

    def open(self, request, timeout=None):
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
    def install(*responses) -> FakeOpener:
        fake = FakeOpener(*responses)

        def build_opener(*handlers):
            fake.handlers = handlers
            return fake

        monkeypatch.setattr(urllib.request, "build_opener", build_opener)
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

    def test_rfc7636_appendix_b_known_answer(self):
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        assert pkce_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"

    def test_pair_challenge_matches_verifier(self):
        verifier, challenge = make_pkce_pair()
        assert pkce_challenge(verifier) == challenge


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

    @pytest.mark.parametrize("state", ["", "   ", None])
    def test_blank_state_rejected(self, state):
        with pytest.raises(OAuthError) as info:
            build_authorize_url(SPEC, client_id="c", redirect_uri="https://r", state=state)  # type: ignore[arg-type]
        assert info.value.error == "state_required"

    def test_extras_cannot_override_flow_params(self):
        spec = OAuthSpec(
            provider="p",
            authorize_url="https://a.example/auth",
            token_url="https://a.example/token",
            extra_authorize_params={
                "state": "fixed",
                "redirect_uri": "https://evil.example/cb",
                "client_id": "other",
                "response_type": "token",
                "code_challenge": "attacker",
                "code_challenge_method": "plain",
                "prompt": "consent",
            },
        )
        query = _query(build_authorize_url(spec, client_id="cid", redirect_uri="https://app/cb", state="mine"))
        assert query == {
            "response_type": "code",
            "client_id": "cid",
            "redirect_uri": "https://app/cb",
            "state": "mine",
            "prompt": "consent",
        }

    def test_baked_in_flow_params_dropped(self):
        spec = OAuthSpec(
            provider="p",
            authorize_url="https://a.example/auth?code_challenge=baked&tenant=common",
            token_url="https://a.example/token",
        )
        query = _query(build_authorize_url(spec, client_id="c", redirect_uri="https://r", state="s"))
        assert "code_challenge" not in query and query["tenant"] == "common"

    def test_flow_params_constant(self):
        assert {"state", "redirect_uri", "client_id", "response_type", "code_challenge"} <= FLOW_AUTHORIZE_PARAMS

    def test_insecure_authorize_url_rejected(self):
        spec = OAuthSpec(provider="p", authorize_url="http://a.example/auth", token_url="https://t")
        with pytest.raises(OAuthError) as info:
            build_authorize_url(spec, client_id="c", redirect_uri="https://r", state="s")
        assert info.value.error == "insecure_url"


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

    def test_omitted_scope_keeps_previous_scope(self, http):
        http(FakeResponse(200, {"access_token": "a"}))
        token = refresh_access_token(
            SPEC, client_id="c", client_secret="s", refresh_token="r", previous_scope="read write"
        )
        assert token.scope == "read write"

    def test_omitted_scope_prefers_requested_scopes(self, http):
        http(FakeResponse(200, {"access_token": "a"}))
        token = refresh_access_token(
            SPEC, client_id="c", client_secret="s", refresh_token="r", scopes=["read"], previous_scope="read write"
        )
        assert token.scope == "read"

    def test_returned_scope_wins(self, http):
        http(FakeResponse(200, {"access_token": "a", "scope": "read"}))
        token = refresh_access_token(
            SPEC, client_id="c", client_secret="s", refresh_token="r", previous_scope="read write"
        )
        assert token.scope == "read"

    def test_toolkit_passes_previous_scope(self, http):
        http(FakeResponse(200, {"access_token": "a"}))
        token = MockToolkit().refresh_access_token(
            client_id="c", client_secret="s", refresh_token="r", previous_scope="items:read"
        )
        assert token.scope == "items:read"


class TestTransportGuards:
    def test_requests_use_a_no_redirect_opener(self, http):
        fake = http(FakeResponse(200, {"access_token": "a"}))
        exchange_code(SPEC, client_id="c", client_secret="s", code="x", redirect_uri="https://r")
        assert len(fake.handlers) == 1
        handler = fake.handlers[0]
        assert issubclass(handler, urllib.request.HTTPRedirectHandler)
        assert handler().redirect_request(None, None, 302, "Found", {}, "https://elsewhere") is None

    def test_3xx_status_is_unexpected_redirect(self, http):
        http(FakeResponse(302, b""))
        with pytest.raises(OAuthError) as info:
            exchange_code(SPEC, client_id="c", client_secret="s", code="x", redirect_uri="https://r")
        assert (info.value.error, info.value.status) == ("unexpected_redirect", 302)

    def test_http_exception_is_oauth_error(self, http):
        http(http_client.IncompleteRead(b"partial", 480))
        with pytest.raises(OAuthError) as info:
            refresh_access_token(SPEC, client_id="c", client_secret="client-secret-xyz", refresh_token="refresh-old-123")
        assert info.value.error == "network_error"
        _assert_no_secrets(str(info.value))

    def test_http_exception_reading_error_body(self, http, monkeypatch):
        class BrokenBody(FakeResponse):
            def read(self):
                raise http_client.IncompleteRead(b"", 10)

        fake = http(FakeResponse(400, b""))

        def open_(request, timeout=None):
            fake.requests.append(request)
            raise urllib.error.HTTPError(request.full_url, 400, "bad", Message(), BrokenBody(400, b""))

        monkeypatch.setattr(fake, "open", open_)
        with pytest.raises(OAuthError) as info:
            exchange_code(SPEC, client_id="c", client_secret="s", code="x", redirect_uri="https://r")
        assert info.value.error == "network_error"

    def test_pkce_exchange_requires_verifier(self, http):
        fake = http()
        with pytest.raises(OAuthError) as info:
            exchange_code(MOCK_OAUTH, client_id="c", client_secret="s", code="x", redirect_uri="https://r")
        assert info.value.error == "pkce_required"
        assert fake.requests == []


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


# --------------------------------------------------------------------------
# Granted scopes and account identity
# --------------------------------------------------------------------------


def _id_token(claims: dict, *, header: dict | None = None) -> str:
    """An unsigned JWT carrying ``claims`` (the signature is not read)."""

    def part(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{part(header or {'alg': 'RS256'})}.{part(claims)}.c2lnbmF0dXJl"


GOOGLE_CLAIMS = {
    "iss": "https://accounts.google.com",
    "aud": "cid",
    "sub": "1234567890",
    "email": "sam@example.com",
    "name": "Sam Doe",
    "hd": "example.com",
}
MICROSOFT_CLAIMS = {
    "aud": "cid",
    "sub": "pairwise-sub",
    "oid": "00000000-0000-0000-0000-00000000beef",
    "tid": "11111111-1111-1111-1111-111111111111",
    "preferred_username": "sam@contoso.example",
    "name": "Sam",
}


class TestGrantedScopes:
    @pytest.mark.parametrize(
        ("scope", "granted"),
        [
            ("read write", ("read", "write")),
            ("repo,read:user", ("repo", "read:user")),  # GitHub and Slack use commas
            ("  a  b\tc ", ("a", "b", "c")),
            ("a a b", ("a", "b")),
            ("https://www.googleapis.com/auth/drive openid", ("https://www.googleapis.com/auth/drive", "openid")),
        ],
    )
    def test_parsed_from_scope(self, scope, granted):
        assert OAuthToken(access_token="t", scope=scope).granted_scopes == granted

    @pytest.mark.parametrize("scope", ["", "   "])
    def test_none_when_the_provider_did_not_say(self, scope):
        assert OAuthToken(access_token="t", scope=scope).granted_scopes is None

    def test_exchange_reports_what_was_granted(self, http):
        # The user unticked "write" on the consent screen.
        http(FakeResponse(200, {"access_token": "access-new-456", "scope": "read"}))
        token = exchange_code(SPEC, client_id="cid", client_secret="s", code="c", redirect_uri="https://app/cb")
        assert token.granted_scopes == ("read",)

    def test_form_encoded_github_scopes(self, http):
        http(FakeResponse(200, "access_token=gho_x&scope=repo%2Cread%3Auser&token_type=bearer", "application/x-www-form-urlencoded"))
        token = exchange_code(GITHUB_LIKE, client_id="cid", client_secret="s", code="c", redirect_uri="https://app/cb")
        assert token.granted_scopes == ("repo", "read:user")

    def test_refresh_without_scope_keeps_the_previous_grant(self, http):
        http(FakeResponse(200, {"access_token": "access-new-456"}))
        token = refresh_access_token(
            SPEC, client_id="cid", client_secret="s", refresh_token="r", previous_scope="read"
        )
        assert token.granted_scopes == ("read",)


class TestAccount:
    def test_google_id_token(self):
        account = account_from_id_token(_id_token(GOOGLE_CLAIMS), client_id="cid")
        assert account == OAuthAccount(
            id="1234567890", email="sam@example.com", name="Sam Doe", tenant="example.com"
        )

    def test_microsoft_id_token_prefers_oid_and_tid(self):
        account = account_from_id_token(_id_token(MICROSOFT_CLAIMS), client_id="cid")
        assert account == OAuthAccount(
            id="00000000-0000-0000-0000-00000000beef",
            email="sam@contoso.example",
            name="Sam",
            tenant="11111111-1111-1111-1111-111111111111",
        )

    def test_username_without_at_is_not_an_email(self):
        claims = {**MICROSOFT_CLAIMS, "preferred_username": "sam"}
        assert account_from_id_token(_id_token(claims)).email is None  # type: ignore[union-attr]

    @pytest.mark.parametrize("aud", ["other-client", ["other-client"], None])
    def test_token_for_another_client_is_ignored(self, aud):
        claims = {**GOOGLE_CLAIMS, "aud": aud}
        assert account_from_id_token(_id_token(claims), client_id="cid") is None

    def test_audience_list_naming_the_client(self):
        claims = {**GOOGLE_CLAIMS, "aud": ["other", "cid"]}
        assert account_from_id_token(_id_token(claims), client_id="cid") is not None

    @pytest.mark.parametrize(
        "id_token",
        [
            None,
            42,
            "",
            "not-a-jwt",
            "a.b",
            "a.%%%.c",
            "a." + base64.urlsafe_b64encode(b"not json").decode() + ".c",
            "a." + base64.urlsafe_b64encode(b"[1, 2]").decode() + ".c",
            "a." + base64.urlsafe_b64encode(b"[" * 5000 + b"]" * 5000).decode() + ".c",
            "a." + base64.urlsafe_b64encode(b"\xff\xfe").decode() + ".c",
            "x" * (16 * 1024 + 1),
            _id_token({"email": "no-subject@example.com"}),
            _id_token({"sub": "   "}),
        ],
    )
    def test_unreadable_id_tokens_give_no_account(self, id_token):
        assert account_from_id_token(id_token) is None

    def test_long_claims_are_capped(self):
        account = account_from_id_token(_id_token({"sub": "s", "name": "n" * 5000}))
        assert account is not None and len(account.name or "") == 320

    def test_exchange_fills_account(self, http):
        http(
            FakeResponse(
                200,
                {
                    "access_token": "access-new-456",
                    "scope": "openid email",
                    "id_token": _id_token(GOOGLE_CLAIMS),
                },
            )
        )
        token = exchange_code(SPEC, client_id="cid", client_secret="s", code="c", redirect_uri="https://app/cb")
        assert token.account is not None and token.account.email == "sam@example.com"

    def test_exchange_drops_an_id_token_for_another_client(self, http):
        http(FakeResponse(200, {"access_token": "a", "id_token": _id_token(GOOGLE_CLAIMS)}))
        token = exchange_code(SPEC, client_id="someone-else", client_secret="s", code="c", redirect_uri="https://app/cb")
        assert token.account is None

    def test_no_id_token_means_no_account_and_the_old_storage_shape(self, http):
        http(FakeResponse(200, {"access_token": "a", "scope": "read"}))
        token = exchange_code(SPEC, client_id="cid", client_secret="s", code="c", redirect_uri="https://app/cb")
        assert token.account is None
        assert set(token.to_dict()) == {"access_token", "refresh_token", "expires_at", "scope", "token_type"}

    def test_storage_round_trip(self):
        account = OAuthAccount(id="u1", email="sam@example.com", name=None, tenant="t1")
        token = OAuthToken(access_token="a", scope="read", account=account)
        stored = token.to_dict()
        assert stored["account"] == {"id": "u1", "email": "sam@example.com", "name": None, "tenant": "t1"}
        assert OAuthToken.from_dict(json.loads(json.dumps(stored))) == token

    @pytest.mark.parametrize("account", [None, {}, {"id": ""}, "u1", []])
    def test_from_dict_ignores_an_unusable_account(self, account):
        assert OAuthToken.from_dict({"access_token": "a", "account": account}).account is None

    def test_account_stays_out_of_repr(self):
        token = OAuthToken(access_token="a", account=OAuthAccount(id="u1", email="sam@example.com"))
        assert "sam@example.com" not in repr(token)

    def test_toolkit_exchange_carries_both(self, http):
        http(
            FakeResponse(
                200,
                {"access_token": "access-new-456", "scope": "items.read", "id_token": _id_token({**GOOGLE_CLAIMS, "aud": "cid"})},
            )
        )
        token = MockToolkit().exchange_code(
            client_id="cid", client_secret="s", code="c", redirect_uri="https://app/cb", code_verifier="v"
        )
        assert token.granted_scopes == ("items.read",)
        assert token.account is not None and token.account.id == "1234567890"
