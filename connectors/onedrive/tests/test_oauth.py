"""The Microsoft identity platform spec against core's flow: ``{tenant}``
placeholder URLs, PKCE, rotating refresh tokens, and no revocation endpoint.

Core posts token requests through ``urllib.request.build_opener(...)``
(redirects are never followed), so the token endpoint is faked by patching
``build_opener`` — never ``urlopen`` — and no socket is opened."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message

import pytest
from aisquare.pipe import OAuthError, make_pkce_pair

from aisquare_pipe_onedrive import MICROSOFT_OAUTH, OneDriveToolkit, microsoft_oauth
from aisquare_pipe_onedrive.toolkit import DEFAULT_TENANT

from .fakes import FakeSession

CLIENT_ID = "00001111-aaaa-2222-bbbb-3333cccc4444"
CLIENT_SECRET = "ms-app-secret~value"
TENANT_ID = "aaaabbbb-0000-cccc-1111-dddd2222eeee"
REDIRECT = "https://pipe.example/v1/connections/callback"
ACCESS = "EwBIA8l6newAccessToken"
REFRESH = "M.C1_rotatedRefreshToken"


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
    """Stands in for the no-redirect opener core builds per token request."""

    def __init__(self, status: int, body: str, seen: list[urllib.request.Request]):
        self.status, self.body, self.seen = status, body, seen

    def open(self, request, timeout=None):
        self.seen.append(request)
        if self.status >= 400:
            raise urllib.error.HTTPError(request.full_url, self.status, "error", Message(), io.BytesIO(self.body.encode()))
        return FakeUrlResponse(self.status, self.body, "application/json; charset=utf-8")


@pytest.fixture
def token_endpoint(monkeypatch):
    seen: list[urllib.request.Request] = []

    def install(status: int, body: dict):
        def build_opener(*handlers):
            return FakeOpener(status, json.dumps(body), seen)

        monkeypatch.setattr(urllib.request, "build_opener", build_opener)
        return seen

    return install


def _token_body(**extra):
    return {
        "token_type": "Bearer",
        "scope": "Files.ReadWrite.All User.Read profile openid email",
        "expires_in": 3599,
        "ext_expires_in": 3599,
        "access_token": ACCESS,
        "refresh_token": REFRESH,
        **extra,
    }


def _form(request: urllib.request.Request) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(request.data.decode()))


class TestSpec:
    def test_endpoints_scopes_and_flags(self):
        assert MICROSOFT_OAUTH.provider == "microsoft"
        assert MICROSOFT_OAUTH.authorize_url == "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
        assert MICROSOFT_OAUTH.token_url == "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
        assert MICROSOFT_OAUTH.default_scopes == ("offline_access", "User.Read", "Files.ReadWrite.All")
        assert MICROSOFT_OAUTH.pkce is True
        assert MICROSOFT_OAUTH.token_expires is True
        assert MICROSOFT_OAUTH.rotates_refresh_token is True
        assert MICROSOFT_OAUTH.revoke_url is None

    @pytest.mark.parametrize(
        "tenant", ["common", "organizations", "consumers", TENANT_ID, "contoso.onmicrosoft.com"]
    )
    def test_microsoft_oauth_fills_the_tenant(self, tenant):
        spec = microsoft_oauth(tenant)
        assert spec.authorize_url == f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
        assert spec.token_url == f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
        assert (spec.pkce, spec.rotates_refresh_token, spec.default_scopes) == (True, True, MICROSOFT_OAUTH.default_scopes)

    def test_default_tenant_is_common(self):
        assert DEFAULT_TENANT == "common"
        assert microsoft_oauth().token_url == "https://login.microsoftonline.com/common/oauth2/v2.0/token"
        assert microsoft_oauth(None) == microsoft_oauth("common")

    @pytest.mark.parametrize("tenant", ["evil.com/x?", "../common", "a b", "common#", "-x", ""])
    def test_rejects_a_tenant_that_could_bend_the_url(self, tenant):
        if tenant == "":
            assert microsoft_oauth(tenant).token_url.endswith("/common/oauth2/v2.0/token")
            return
        with pytest.raises(ValueError):
            microsoft_oauth(tenant)

    def test_filled_spec_is_unchanged_by_a_second_fill(self):
        spec = microsoft_oauth(TENANT_ID)
        assert microsoft_oauth("common", spec) is spec

    def test_pipeaas_style_instance_override(self):
        # pipeAAS fills '{tenant}' itself and assigns the spec to the instance.
        toolkit = OneDriveToolkit()
        toolkit.oauth = microsoft_oauth("organizations")
        url = toolkit.build_authorize_url(client_id=CLIENT_ID, redirect_uri=REDIRECT, state="s", code_challenge="c" * 43)
        assert url.startswith("https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize?")
        assert OneDriveToolkit.oauth is MICROSOFT_OAUTH  # the class spec is untouched


class TestAuthorize:
    def test_url_with_pkce(self):
        _verifier, challenge = make_pkce_pair()
        url = OneDriveToolkit().build_authorize_url(client_id=CLIENT_ID, redirect_uri=REDIRECT, state="st4te", code_challenge=challenge)
        parts = urllib.parse.urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
        assert dict(urllib.parse.parse_qsl(parts.query)) == {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT,
            "state": "st4te",
            "scope": "offline_access User.Read Files.ReadWrite.All",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }

    def test_tenant_argument(self):
        url = OneDriveToolkit(tenant=TENANT_ID).build_authorize_url(client_id=CLIENT_ID, redirect_uri=REDIRECT, state="s", code_challenge="c" * 43)
        assert url.startswith(f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/authorize?")

    def test_pkce_is_required(self):
        with pytest.raises(OAuthError) as exc:
            OneDriveToolkit().build_authorize_url(client_id=CLIENT_ID, redirect_uri=REDIRECT, state="s")
        assert exc.value.error == "pkce_required"

    def test_custom_scopes(self):
        url = OneDriveToolkit().build_authorize_url(
            client_id=CLIENT_ID, redirect_uri=REDIRECT, state="s", code_challenge="c" * 43, scopes=["offline_access", "Files.Read"]
        )
        assert dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))["scope"] == "offline_access Files.Read"


class TestExchangeAndRefresh:
    def test_exchange_code(self, token_endpoint):
        seen = token_endpoint(200, _token_body())
        token = OneDriveToolkit(tenant="organizations").exchange_code(
            client_id=CLIENT_ID, client_secret=CLIENT_SECRET, code="0.AXcode", redirect_uri=REDIRECT, code_verifier="v" * 64
        )
        assert (token.access_token, token.refresh_token) == (ACCESS, REFRESH)
        assert token.expires_at is not None and not token.expired()
        assert seen[0].full_url == "https://login.microsoftonline.com/organizations/oauth2/v2.0/token"
        form = _form(seen[0])
        assert form["grant_type"] == "authorization_code"
        assert form["code_verifier"] == "v" * 64
        assert form["client_secret"] == CLIENT_SECRET

    def test_exchange_needs_the_verifier(self):
        with pytest.raises(OAuthError) as exc:
            OneDriveToolkit().exchange_code(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, code="c", redirect_uri=REDIRECT)
        assert exc.value.error == "pkce_required"

    def test_unfilled_tenant_posts_to_common(self, token_endpoint):
        seen = token_endpoint(200, _token_body())
        OneDriveToolkit().exchange_code(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, code="c", redirect_uri=REDIRECT, code_verifier="v" * 64)
        assert seen[0].full_url == "https://login.microsoftonline.com/common/oauth2/v2.0/token"

    def test_refresh_rotates_the_refresh_token(self, token_endpoint):
        seen = token_endpoint(200, _token_body(refresh_token="M.C1_next"))
        token = OneDriveToolkit().refresh_access_token(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, refresh_token=REFRESH)
        assert token.refresh_token == "M.C1_next"
        form = _form(seen[0])
        assert (form["grant_type"], form["refresh_token"]) == ("refresh_token", REFRESH)
        assert "scope" not in form  # Microsoft keeps the consented scopes

    def test_invalid_grant_is_an_oauth_error_without_secrets(self, token_endpoint):
        token_endpoint(400, {"error": "invalid_grant", "error_description": "AADSTS70000: The refresh token has expired."})
        with pytest.raises(OAuthError) as exc:
            OneDriveToolkit().refresh_access_token(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, refresh_token=REFRESH)
        assert exc.value.error == "invalid_grant"
        assert CLIENT_SECRET not in str(exc.value) and REFRESH not in str(exc.value)


class TestRevoke:
    @pytest.mark.parametrize("secret", [CLIENT_SECRET, None, "sécret-ünïcode-✓"])
    def test_revoke_forgets_and_sends_nothing(self, secret, monkeypatch):
        def no_opener(*handlers):
            raise AssertionError("revoke must not call any endpoint")

        monkeypatch.setattr(urllib.request, "build_opener", no_opener)
        session = FakeSession()
        toolkit = OneDriveToolkit(session=session)  # type: ignore[arg-type]
        assert toolkit.revoke_token(client_id=CLIENT_ID, client_secret=secret, token=ACCESS) is False
        assert toolkit.revoke_token(client_id=CLIENT_ID, client_secret=secret, token=REFRESH, token_type_hint="refresh_token") is False
        assert session.calls == []
