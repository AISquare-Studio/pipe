"""Router metadata: the connector card, each tool's OAuth scopes (its
existing per-identity map, promoted) and risk (its tier, through the flags),
and listing and refusing by the connection's granted scopes — the user
token's scopes as the user, the bot token's as the bot."""

from __future__ import annotations

import pytest
from aisquare.pipe import CATEGORIES

from aisquare_pipe_slack import SlackToolkit
from aisquare_pipe_slack.catalog import CATALOG, TOOLS
from aisquare_pipe_slack.scopes import BOT_SCOPES, USER_SCOPES

from .fakes import ALL_TIERS, BOT_CONFIG, USER_CONFIG, harness, ok

RISK_OF_TIER = {"read": "read", "send": "write", "write": "write", "destructive": "destructive"}


class TestCard:
    def test_card(self):
        card = SlackToolkit.card()
        assert (card["name"], card["category"]) == ("slack", "chat")
        assert card["category"] in CATEGORIES
        assert 2 <= len(card["examples"]) <= 4
        assert {"slack", "channel", "DM"} <= set(card["keywords"])


class TestPromotedMetadata:
    def test_required_scopes_are_the_default_identitys(self):
        for row in CATALOG:
            expected = row.user_scopes if row.user_scopes is not None else row.bot_scopes
            assert row.spec.required_scopes == expected, row.spec.slug

    def test_bot_only_tools_carry_their_bot_scopes(self):
        bot_only = [row for row in CATALOG if row.user_scopes is None]
        assert bot_only
        assert all(row.spec.required_scopes == row.bot_scopes for row in bot_only)

    def test_risk_follows_the_tier(self):
        for row in CATALOG:
            overwrites = row.tier == "write" and row.spec.destructive  # edits
            assert row.spec.risk == ("destructive" if overwrites else RISK_OF_TIER[row.tier]), row.spec.slug

    def test_the_consent_scopes_cover_every_tool(self):
        toolkit = SlackToolkit()
        user = {**USER_CONFIG, "enabled_tiers": ALL_TIERS}
        bot = {**BOT_CONFIG, "enabled_tiers": ALL_TIERS}
        assert toolkit.available_tools({**user, "granted_scopes": list(USER_SCOPES)}) == toolkit.available_tools(user)
        assert toolkit.available_tools({**bot, "granted_scopes": list(BOT_SCOPES)}) == toolkit.available_tools(bot)


class TestGrantedScopes:
    def test_no_grant_changes_nothing(self):
        toolkit = SlackToolkit()
        assert toolkit.available_tools(USER_CONFIG) == toolkit.available_tools({**USER_CONFIG, "granted_scopes": None})

    def test_user_listing_follows_the_grant(self):
        granted = {"chat:write", "channels:read", "groups:read"}
        listed = SlackToolkit().available_tools({**USER_CONFIG, "granted_scopes": sorted(granted)})
        assert all(set(TOOLS[t.slug].user_scopes or ()) <= granted for t in listed)
        assert "SLACK_SEND_MESSAGE" in {t.slug for t in listed}
        assert "SLACK_SEARCH_MESSAGES" not in {t.slug for t in listed}  # needs search:read
        only_chat = SlackToolkit().available_tools({**USER_CONFIG, "granted_scopes": "chat:write"})
        assert "SLACK_SEND_MESSAGE" not in {t.slug for t in only_chat}  # also looks up channel names

    def test_bot_connections_are_checked_against_bot_scopes(self):
        """A bot token holds bot scopes; the user list must not decide."""
        row = next(r for r in CATALOG if r.user_scopes and r.bot_scopes and r.user_scopes != r.bot_scopes and r.tier == "send")
        slug = row.spec.slug
        toolkit = SlackToolkit()
        assert slug in {t.slug for t in toolkit.available_tools({**BOT_CONFIG, "granted_scopes": list(row.bot_scopes)})}
        user_only = sorted(set(row.user_scopes) - set(row.bot_scopes))
        if user_only:
            listed = toolkit.available_tools({**BOT_CONFIG, "granted_scopes": user_only})
            assert slug not in {t.slug for t in listed}

    def test_bot_identity_lists_send_tools_with_the_bot_grant(self):
        listed = SlackToolkit().available_tools({**BOT_CONFIG, "granted_scopes": list(BOT_SCOPES)})
        assert {t.slug for t in listed} == {t.slug for t in SlackToolkit().available_tools(BOT_CONFIG)}

    def test_an_ungranted_tool_is_refused_before_any_request(self):
        h = harness()
        result = h.call("SLACK_SEND_MESSAGE", {"channel": "C0123456", "markdown_text": "hi"}, granted_scopes="channels:read")
        assert result.error_code == "insufficient_scope"
        assert h.session.calls == []

    def test_a_granted_tool_runs(self):
        h = harness(ok(channel="C0123456", ts="1700000000.000100", message={"text": "hi"}))
        result = h.call(
            "SLACK_SEND_MESSAGE",
            {"channel": "C0123456", "markdown_text": "hi"},
            config=BOT_CONFIG,
            granted_scopes=list(BOT_SCOPES),
        )
        assert result.successful, result.error

    def test_the_gate_still_comes_first(self):
        """A tier the connection has not enabled stays ``forbidden``, scopes or not."""
        h = harness()
        result = h.call("SLACK_DELETES_A_MESSAGE_FROM_A_CHAT", {"channel": "C0123456", "ts": "1.2"}, granted_scopes=list(USER_SCOPES))
        assert result.error_code == "forbidden"
        assert h.session.calls == []

    @pytest.mark.parametrize("config", [None, {}, {"identity": "robot"}, "not a mapping"])
    def test_odd_configs_never_raise(self, config):
        toolkit = SlackToolkit()
        spec = toolkit.tools()[0]
        assert isinstance(toolkit.missing_scopes(spec, config), list)  # type: ignore[arg-type]
