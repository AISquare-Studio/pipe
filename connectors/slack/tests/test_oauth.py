"""OAuth: the authorize URL per identity and scope selection, the code
exchange (every response shape), refresh, and revoke."""

from __future__ import annotations

from typing import ClassVar

import pytest
import requests
from aisquare.pipe import OAuthError

from aisquare_pipe_slack import (
    BOT_SCOPES,
    SCOPE_GROUPS,
    SLACK_OAUTH,
    USER_SCOPES,
    SlackToolkit,
    scopes_for,
)
from aisquare_pipe_slack.catalog import TOOLS
from aisquare_pipe_slack.scopes import AREAS, resolve_scopes

from .fakes import BOT_TOKEN, USER_TOKEN, FakeSession, ok, query, response, slack_error

AUTH = {"client_id": "123.456", "redirect_uri": "https://api.example.test/v1/connections/callback", "state": "st8"}
EXCHANGE = {"client_id": "123.456", "client_secret": "s3cret-client", "code": "c0de", "redirect_uri": AUTH["redirect_uri"]}

#: oauth.v2.access for a consent with bot and user scopes.
BOTH = {
    "ok": True,
    "access_token": BOT_TOKEN,
    "token_type": "bot",
    "scope": "chat:write,channels:read",
    "bot_user_id": "U0BOT1234",
    "app_id": "A0123",
    "team": {"id": "T0123", "name": "Acme"},
    "enterprise": None,
    "authed_user": {"id": "U0123456", "scope": "search:read,chat:write", "access_token": USER_TOKEN, "token_type": "user"},
}
#: User scopes only: the token sits under authed_user alone.
USER_ONLY = {
    "ok": True,
    "app_id": "A0123",
    "team": {"id": "T0123"},
    "authed_user": {"id": "U0123456", "scope": "search:read", "access_token": USER_TOKEN, "token_type": "user"},
}
#: oauth.v2.user.access / a user refresh: the user token at the top level.
USER_TOP = {"ok": True, "access_token": USER_TOKEN, "token_type": "user", "scope": "search:read", "team": {"id": "T0123"}}


def _toolkit(*responses) -> tuple[SlackToolkit, FakeSession]:
    session = FakeSession(list(responses))
    return SlackToolkit(session=session), session  # type: ignore[arg-type]


class TestSpec:
    def test_spec(self):
        assert SLACK_OAUTH.provider == "slack"
        assert SLACK_OAUTH.authorize_url == "https://slack.com/oauth/v2/authorize"
        assert SLACK_OAUTH.token_url == "https://slack.com/api/oauth.v2.access"
        assert (SLACK_OAUTH.pkce, SLACK_OAUTH.token_expires, SLACK_OAUTH.rotates_refresh_token) == (False, False, False)
        assert SLACK_OAUTH.scope_separator == ","


class TestAuthorizeUrl:
    def test_both_identities_by_default_with_every_scope(self):
        # One consent mints both tokens, so switching config['identity']
        # later needs no re-consent (owner Q5, manager seq 4660).
        url = SlackToolkit().build_authorize_url(**AUTH)
        params = query(url)
        assert url.startswith("https://slack.com/oauth/v2/authorize?")
        assert params["user_scope"] == ",".join(USER_SCOPES)
        assert params["scope"] == ",".join(BOT_SCOPES)
        assert {k: params[k] for k in ("client_id", "redirect_uri", "state")} == AUTH

    def test_user_identity(self):
        params = query(SlackToolkit().build_authorize_url(**AUTH, identity="user"))
        assert params["user_scope"] == ",".join(USER_SCOPES)
        assert "scope" not in params

    def test_bot_identity(self):
        params = query(SlackToolkit().build_authorize_url(**AUTH, identity="bot"))
        assert params["scope"] == ",".join(BOT_SCOPES)
        assert "user_scope" not in params

    def test_both_in_one_consent(self):
        params = query(SlackToolkit().build_authorize_url(**AUTH, identity="both"))
        assert params["user_scope"] == ",".join(USER_SCOPES)
        assert params["scope"] == ",".join(BOT_SCOPES)

    def test_a_scope_subset(self):
        params = query(SlackToolkit().build_authorize_url(**AUTH, scopes=["search", "reactions:read"], identity="user"))
        assert params["user_scope"] == ",".join(sorted({*SCOPE_GROUPS["search"]["user"], "reactions:read"}))
        assert "chat:write" not in params["user_scope"]

    def test_state_is_required(self):
        with pytest.raises(OAuthError) as exc:
            SlackToolkit().build_authorize_url(**{**AUTH, "state": " "})
        assert exc.value.error == "state_required"

    def test_unknown_identity(self):
        with pytest.raises(OAuthError) as exc:
            SlackToolkit().build_authorize_url(**AUTH, identity="admin")  # type: ignore[arg-type]
        assert exc.value.error == "invalid_request"


class TestScopes:
    def test_defaults_are_every_scope_the_catalog_uses(self):
        assert USER_SCOPES == tuple(sorted({s for t in TOOLS.values() for s in t.user_scopes}))
        assert BOT_SCOPES == tuple(sorted({s for t in TOOLS.values() for s in t.bot_scopes or ()}))
        assert scopes_for(None, "both") == (USER_SCOPES, BOT_SCOPES)

    def test_groups(self):
        assert set(SCOPE_GROUPS) == {*AREAS, "read", "send", "write", "destructive"}
        assert AREAS == ("messages", "history", "search", "conversations", "dms", "reactions", "pins", "users", "workspace")
        assert SCOPE_GROUPS["search"]["user"] == (
            "channels:read", "groups:read", "im:read", "mpim:read", "search:read", "users:read", "users:read.email",
        )
        assert SCOPE_GROUPS["search"]["bot"] == (
            "channels:read", "groups:read", "im:read", "mpim:read", "users:read", "users:read.email",
        )
        assert "search:read" not in SCOPE_GROUPS["read"]["bot"]

    def test_every_tool_runs_with_its_area_group_and_its_tier_group(self):
        for slug, tool in TOOLS.items():
            for group in (tool.area, tool.tier):
                assert set(tool.user_scopes) <= set(SCOPE_GROUPS[group]["user"]), (slug, group)
                assert set(tool.bot_scopes or ()) <= set(SCOPE_GROUPS[group]["bot"]), (slug, group)

    def test_read_and_send_tiers_ask_for_less(self):
        user, bot = scopes_for(["read", "send"], "both")
        assert "pins:write" not in user and "reactions:write" not in user
        assert "chat:write" in user and "chat:write" in bot
        assert set(user) < set(USER_SCOPES)

    def test_raw_scopes_go_to_the_sides_that_use_them(self):
        assert scopes_for(["search:read", "chat:write"], "both") == (("chat:write", "search:read"), ("chat:write",))
        assert scopes_for("pins, chat:write", "user") == (("channels:read", "chat:write", "groups:read", "pins:read", "pins:write"), ())

    @pytest.mark.parametrize(
        "selection, identity",
        [
            (["nope"], "user"),
            (["admin:read"], "user"),
            (["search:read"], "bot"),
            (["chat:write.public"], "user"),
            ([], "user"),
            ([" "], "bot"),
        ],
    )
    def test_unknown_or_empty_selections_are_refused(self, selection, identity):
        with pytest.raises(OAuthError) as exc:
            scopes_for(selection, identity)
        assert exc.value.error == "invalid_scope"

    def test_a_group_with_nothing_for_the_bot(self):
        with pytest.raises(OAuthError):
            resolve_scopes(["search:read"], "bot")
        assert resolve_scopes(["search"], "bot")  # the composites in "search" work for bots


class TestExchange:
    def test_posts_the_code_with_client_credentials(self):
        toolkit, session = _toolkit(response(200, USER_ONLY))
        toolkit.exchange_code(**EXCHANGE)
        call = session.last
        assert call.url == "https://slack.com/api/oauth.v2.access"
        assert call.data == {
            "grant_type": "authorization_code",
            "code": "c0de",
            "redirect_uri": EXCHANGE["redirect_uri"],
            "client_id": "123.456",
            "client_secret": "s3cret-client",
        }
        assert call.allow_redirects is False
        assert "Authorization" not in call.headers

    @pytest.mark.parametrize("payload", [USER_ONLY, USER_TOP, BOTH])
    def test_the_user_token_by_default(self, payload):
        token = _toolkit(response(200, payload))[0].exchange_code(**EXCHANGE)
        assert token.access_token == USER_TOKEN
        assert token.token_type == "user"
        assert token.expires_at is None and token.refresh_token is None

    def test_the_bot_token_on_request_or_when_alone(self):
        assert _toolkit(response(200, BOTH))[0].exchange_code(**EXCHANGE, identity="bot").access_token == BOT_TOKEN
        bot_only = {k: v for k, v in BOTH.items() if k != "authed_user"}
        assert _toolkit(response(200, bot_only))[0].exchange_code(**EXCHANGE).access_token == BOT_TOKEN

    def test_a_missing_identity_is_an_error(self):
        with pytest.raises(OAuthError) as exc:
            _toolkit(response(200, USER_ONLY))[0].exchange_code(**EXCHANGE, identity="bot")
        assert exc.value.error == "invalid_token_response"

    def test_installation_has_both_tokens_and_ids(self):
        installation = _toolkit(response(200, BOTH))[0].exchange_installation(**EXCHANGE)
        assert installation.user_token is not None and installation.user_token.access_token == USER_TOKEN
        assert installation.user_token.scope == "search:read,chat:write"
        assert installation.bot_token is not None and installation.bot_token.access_token == BOT_TOKEN
        assert (installation.team_id, installation.user_id, installation.bot_user_id, installation.app_id) == (
            "T0123", "U0123456", "U0BOT1234", "A0123",
        )
        assert installation.enterprise_id is None
        assert USER_TOKEN not in repr(installation) and BOT_TOKEN not in repr(installation)

    @pytest.mark.parametrize(
        "reply, code",
        [
            (response(200, {"ok": False, "error": "invalid_code"}), "invalid_code"),
            (response(200, {"ok": False, "error": "bad_redirect_uri"}), "bad_redirect_uri"),
            (response(200, {"ok": False, "error": "code c0de expired"}), "unknown_error"),
            (response(400, {"ok": False, "error": "<b>"}), "http_400"),
            (response(200, {"ok": True}), "invalid_token_response"),
            (response(200, content=b"<html>"), "invalid_token_response"),
            (response(500, content=b"{}"), "http_500"),
            (response(503, {"ok": False}), "http_503"),
            (response(302, headers={"Location": "https://evil.test"}), "unexpected_redirect"),
            (requests.ConnectionError("https://slack.com/api/oauth.v2.access?code=c0de"), "network_error"),
        ],
    )
    def test_failures(self, reply, code):
        with pytest.raises(OAuthError) as exc:
            _toolkit(reply)[0].exchange_code(**EXCHANGE)
        assert (exc.value.error, exc.value.provider) == (code, "slack")
        assert "s3cret-client" not in str(exc.value) and "c0de" not in str(exc.value)

    def test_owned_session_is_closed(self, monkeypatch):
        session = FakeSession([response(200, USER_ONLY)])
        monkeypatch.setattr(requests, "Session", lambda: session)
        SlackToolkit().exchange_code(**EXCHANGE)
        assert session.closed


class TestRefresh:
    REFRESH: ClassVar[dict[str, str]] = {"client_id": "123.456", "client_secret": "s3cret-client", "refresh_token": "xoxe-1-old"}

    def test_rotation_replaces_the_refresh_token(self):
        payload = {**USER_TOP, "access_token": "xoxe.xoxp-new", "refresh_token": "xoxe-1-new", "expires_in": 43200}
        toolkit, session = _toolkit(response(200, payload))
        token = toolkit.refresh_access_token(**self.REFRESH)
        assert (token.access_token, token.refresh_token) == ("xoxe.xoxp-new", "xoxe-1-new")
        assert token.expires_at is not None
        assert session.last.data["grant_type"] == "refresh_token"
        assert session.last.data["refresh_token"] == "xoxe-1-old"

    def test_a_missing_refresh_token_and_scope_are_kept(self):
        payload = {"ok": True, "authed_user": {"access_token": "xoxe.xoxp-new", "token_type": "user"}}
        token = _toolkit(response(200, payload))[0].refresh_access_token(**self.REFRESH, previous_scope="search:read")
        assert (token.refresh_token, token.scope) == ("xoxe-1-old", "search:read")

    @pytest.mark.parametrize("slack_code", ["invalid_refresh_token", "invalid_grant"])
    def test_a_dead_refresh_token_is_invalid_grant(self, slack_code):
        with pytest.raises(OAuthError) as exc:
            _toolkit(response(200, {"ok": False, "error": slack_code}))[0].refresh_access_token(**self.REFRESH)
        assert exc.value.error == "invalid_grant"


class TestRevoke:
    REVOKE: ClassVar[dict[str, str]] = {"client_id": "123.456", "client_secret": "s3cret-client"}

    def test_revokes_a_user_token_with_bearer(self):
        toolkit, session = _toolkit(ok(user_id="U0123456"), ok(revoked=True))
        assert toolkit.revoke_token(**self.REVOKE, token=USER_TOKEN) is True
        assert session.methods == ["auth.test", "auth.revoke"]
        for call in session.calls:
            assert call.headers["Authorization"] == f"Bearer {USER_TOKEN}"
            assert call.data is None

    def test_never_revokes_a_bot_token(self):
        toolkit, session = _toolkit(ok(user_id="U0BOT1234", bot_id="B0123"))
        assert toolkit.revoke_token(**self.REVOKE, token=BOT_TOKEN) is False
        assert session.methods == ["auth.test"]

    @pytest.mark.parametrize(
        "replies",
        [
            [slack_error("invalid_auth")],
            [ok(user_id="U1"), slack_error("invalid_auth")],
            [ok(user_id="U1"), ok(revoked=False)],
            [ok(user_id="U1"), response(500, content=b"x")],
            [requests.ConnectionError("x")],
            [ok(user_id="U1"), RuntimeError("boom")],
        ],
    )
    def test_anything_else_is_false_and_never_raises(self, replies):
        toolkit, _ = _toolkit(*replies)
        assert toolkit.revoke_token(**self.REVOKE, token=USER_TOKEN) is False
