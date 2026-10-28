"""``slack-app-manifest.json`` stays in sync with the catalog: the app asks
for exactly the scopes the tools declare — so no tool ships needing a scope
the app does not request, and the app requests nothing no tool uses."""

from __future__ import annotations

import json
import re
from pathlib import Path

from aisquare_pipe_slack import BOT_SCOPES, USER_SCOPES
from aisquare_pipe_slack.catalog import TOOLS
from aisquare_pipe_slack.constants import TOKEN_ROTATION

MANIFEST = json.loads((Path(__file__).parent.parent / "slack-app-manifest.json").read_text())
#: Each method's scopes as docs.slack.dev's page header lists them (frozen
#: 2026-10-28 from https://docs.slack.dev/reference/methods/<method>.md).
#: Refresh it when a tool starts calling a new method.
DOCS = json.loads((Path(__file__).parent / "fixtures" / "slack_method_scopes.json").read_text())
#: A tool resolving a channel *name* also calls this method; these are the
#: arguments that take one.
LOOKUP_METHOD = "conversations.list"
CHANNEL_ARGS = {"channel", "channel_id", "channels", "channel_ids"}
#: Scopes a tool may declare beyond its methods' headers, and why.
FEATURE_SCOPES = {
    ("SLACK_SEND_MESSAGE", "bot", "chat:write.public"): "lets the bot post to public channels it is not in",
    ("SLACK_LIST_ALL_USERS", "user", "users:read.email"): "users.list returns email only with it",
    ("SLACK_LIST_ALL_USERS", "bot", "users:read.email"): "users.list returns email only with it",
}
SCOPES = MANIFEST["oauth_config"]["scopes"]
SCOPE_NAME = re.compile(r"^[a-z_]+(\.[a-z_]+)?:[a-z_]+(\.[a-z_]+)?$")


def test_user_scopes_equal_what_the_tools_declare():
    declared = {s for tool in TOOLS.values() for s in tool.user_scopes}
    assert SCOPES["user"] == sorted(declared) == list(USER_SCOPES)


def test_bot_scopes_equal_what_the_tools_declare():
    declared = {s for tool in TOOLS.values() for s in tool.bot_scopes or ()}
    assert SCOPES["bot"] == sorted(declared) == list(BOT_SCOPES)


def test_scope_names_are_well_formed():
    for scope in [*SCOPES["user"], *SCOPES["bot"]]:
        assert SCOPE_NAME.match(scope), scope


def test_bot_scopes_leave_out_user_only_and_unsure_scopes():
    # search:read and users.profile:write are user-token scopes; mpim:write's
    # docs disagree on bot support, and an invalid bot scope makes the
    # manifest fail to save.
    for scope in ("search:read", "users.profile:write", "mpim:write", "channels:write"):
        assert scope not in SCOPES["bot"]


def test_the_manifest_is_an_app_agnostic_template():
    # Q3: pipe doesn't own the app; the host fills in its own values.
    assert MANIFEST["display_information"]["name"] == "YOUR_APP_NAME"
    assert MANIFEST["features"]["bot_user"]["display_name"] == "your-app-bot"
    assert MANIFEST["oauth_config"]["redirect_urls"] == ["https://YOUR_HOST/v1/connections/callback"]
    assert "AISquare" not in json.dumps(MANIFEST)


def test_bot_user_and_settings():
    assert MANIFEST["features"]["bot_user"]["display_name"]
    assert MANIFEST["oauth_config"]["pkce_enabled"] is False
    assert MANIFEST["settings"]["token_rotation_enabled"] is TOKEN_ROTATION is False
    assert all(url.startswith("https://") for url in MANIFEST["oauth_config"]["redirect_urls"])
    assert 174 <= len(MANIFEST["display_information"]["long_description"]) <= 4000


def test_the_readme_lists_every_tool_with_its_tier():
    readme = (Path(__file__).parent.parent / "README.md").read_text()
    for slug, tool in TOOLS.items():
        assert f"| `{slug}` | {tool.tier} |" in readme, slug


def test_the_docs_fixture_covers_every_method():
    assert DOCS["missing"] == []
    used = {m for tool in TOOLS.values() for m in tool.methods} | {LOOKUP_METHOD}
    assert used <= set(DOCS["methods"])


def test_every_declared_scope_is_documented_for_its_methods():
    # Plan §6.2 test_manifest (d): a tool can't declare a scope Slack doesn't
    # list for the methods it calls, unless FEATURE_SCOPES says why.
    problems = []
    for slug, tool in TOOLS.items():
        methods = list(tool.methods)
        if CHANNEL_ARGS & set(tool.spec.input_schema["properties"]):
            methods.append(LOOKUP_METHOD)
        for side, declared in (("user", tool.user_scopes), ("bot", tool.bot_scopes or ())):
            documented = {scope for m in methods for scope in DOCS["methods"][m][side]}
            for scope in declared:
                if scope not in documented and (slug, side, scope) not in FEATURE_SCOPES:
                    problems.append(f"{slug} {side}: {scope}")
    assert problems == []


def test_feature_scopes_are_still_needed():
    for slug, side, scope in FEATURE_SCOPES:
        declared = TOOLS[slug].user_scopes if side == "user" else TOOLS[slug].bot_scopes or ()
        assert scope in declared, (slug, side, scope)
        documented = {s for m in TOOLS[slug].methods for s in DOCS["methods"][m][side]}
        assert scope not in documented, (slug, side, scope)
