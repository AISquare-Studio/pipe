"""Router metadata: the connector card, each tool's OAuth scopes and risk,
listing and refusing by the connection's granted scopes (with GitHub's
scope hierarchy), and grant revocation."""

from __future__ import annotations

import pytest
import requests
from aisquare.pipe import CATEGORIES

from aisquare_pipe_github import GitHubToolkit
from aisquare_pipe_github.rest import revoke_grant
from aisquare_pipe_github.tools import (
    NOTIFICATIONS_SCOPE,
    READ_ORG_SCOPE,
    READ_USER_SCOPE,
    REPO_SCOPE,
)

from .fakes import CONFIG, TOKEN, FakeSession, harness, response

CLIENT_ID = "Iv1.0123456789abcdef"
CLIENT_SECRET = "app-client-secret"


class TestCard:
    def test_card(self):
        card = GitHubToolkit.card()
        assert card["name"] == "github"
        assert card["category"] == "code" and card["category"] in CATEGORIES
        assert 2 <= len(card["examples"]) <= 4
        assert {"pull request", "issue", "repo"} <= set(card["keywords"])


class TestToolScopes:
    def test_every_tool_declares_its_scopes(self):
        specs = {t.slug: t for t in GitHubToolkit().tools()}
        assert specs["GITHUB_GET_THE_AUTHENTICATED_USER"].required_scopes == (READ_USER_SCOPE,)
        assert specs["GITHUB_LIST_ORGANIZATIONS_FOR_THE_AUTHENTICATED_USER"].required_scopes == (READ_ORG_SCOPE,)
        assert specs["GITHUB_LIST_NOTIFICATIONS_FOR_THE_AUTHENTICATED_USER"].required_scopes == (NOTIFICATIONS_SCOPE,)
        others = set(specs) - {
            "GITHUB_GET_THE_AUTHENTICATED_USER",
            "GITHUB_LIST_ORGANIZATIONS_FOR_THE_AUTHENTICATED_USER",
            "GITHUB_LIST_NOTIFICATIONS_FOR_THE_AUTHENTICATED_USER",
        }
        assert all(specs[s].required_scopes == (REPO_SCOPE,) for s in others)

    def test_risk_follows_the_flags(self):
        for tool in GitHubToolkit().tools():
            assert tool.risk == ("destructive" if tool.destructive else "read" if tool.read_only else "write")

    def test_the_default_consent_covers_every_tool(self):
        toolkit = GitHubToolkit()
        assert toolkit.oauth is not None
        granted = {**CONFIG, "granted_scopes": list(toolkit.oauth.default_scopes)}
        assert toolkit.available_tools(granted) == toolkit.tools()


class TestGrantedScopes:
    @pytest.mark.parametrize(
        ("granted", "listed"),
        [
            ("repo", 29),  # everything but the profile and org list
            ("repo,read:user,read:org", 31),  # GitHub's comma-separated form
            ("repo user admin:org", 31),  # broader scopes cover read:user / read:org
            ("read:user", 1),
            ("public_repo", 0),  # narrower than repo: private repos would fail
            ("", 31),  # blank: not known, nothing filtered
        ],
    )
    def test_listing_follows_the_grant(self, granted, listed):
        assert len(GitHubToolkit().available_tools({**CONFIG, "granted_scopes": granted})) == listed

    def test_notifications_are_covered_by_repo(self):
        slugs = {t.slug for t in GitHubToolkit().available_tools({**CONFIG, "granted_scopes": ["repo"]})}
        assert "GITHUB_LIST_NOTIFICATIONS_FOR_THE_AUTHENTICATED_USER" in slugs
        assert "GITHUB_GET_THE_AUTHENTICATED_USER" not in slugs

    def test_an_ungranted_tool_is_refused_before_any_request(self):
        h = harness()
        result = h.call("GITHUB_CREATE_AN_ISSUE", {"owner": "o", "repo": "r", "title": "t"}, config={**CONFIG, "granted_scopes": "read:user"})
        assert result.error_code == "insufficient_scope"
        assert h.session.calls == []

    def test_a_granted_tool_runs(self):
        h = harness(response(200, {"login": "octocat"}))
        result = h.call("GITHUB_GET_THE_AUTHENTICATED_USER", {}, config={**CONFIG, "granted_scopes": "user"})
        assert result.successful, result.error


class TestRevokeGrant:
    def _toolkit(self, *answers):
        session = FakeSession(list(answers))
        return GitHubToolkit(session=session), session  # type: ignore[arg-type]

    def test_deletes_the_grant_with_basic_auth(self):
        toolkit, session = self._toolkit(response(204))
        assert toolkit.revoke_grant(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, token=TOKEN) is True
        call = session.last
        assert (call.method, call.url) == ("DELETE", f"https://api.github.com/applications/{CLIENT_ID}/grant")
        assert call.auth == (CLIENT_ID, CLIENT_SECRET)
        assert call.json == {"access_token": TOKEN}
        assert "Authorization" not in call.headers

    def test_revoke_token_still_revokes_only_the_token(self):
        toolkit, session = self._toolkit(response(204))
        assert toolkit.revoke_token(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, token=TOKEN) is True
        assert session.last.url.endswith("/token")

    def test_without_a_client_secret_nothing_is_sent(self):
        toolkit, session = self._toolkit()
        assert toolkit.revoke_grant(client_id=CLIENT_ID, client_secret=None, token=TOKEN) is False
        assert session.calls == []

    @pytest.mark.parametrize("answer", [response(404, {"message": "Not Found"}), response(422, {"message": "Validation Failed"}), requests.ConnectionError("down"), requests.Timeout("slow")])
    def test_failure_is_false(self, answer):
        toolkit, _ = self._toolkit(answer)
        assert toolkit.revoke_grant(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, token=TOKEN) is False

    def test_never_raises_on_odd_credentials(self):
        # A real Session encodes Basic auth as latin-1 before any socket opens.
        toolkit = GitHubToolkit(session=requests.Session())
        assert toolkit.revoke_grant(client_id=CLIENT_ID, client_secret="sécret-✓", token=TOKEN) is False

    def test_client_id_is_one_path_segment(self):
        session = FakeSession([response(204)])
        assert revoke_grant(client_id="a/../b", client_secret="s", access_token=TOKEN, session=session) is True  # type: ignore[arg-type]
        assert session.last.url == "https://api.github.com/applications/a%2F..%2Fb/grant"

    def test_logs_carry_no_token(self, caplog):
        caplog.set_level("DEBUG")
        toolkit, _ = self._toolkit(response(500, {"message": "boom"}))
        toolkit.revoke_grant(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, token=TOKEN)
        assert TOKEN not in caplog.text and CLIENT_SECRET not in caplog.text
