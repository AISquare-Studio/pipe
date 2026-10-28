"""``slack-app-manifest.json`` stays in sync with the catalog: the app asks
for exactly the scopes the tools declare — so no tool ships needing a scope
the app does not request, and the app requests nothing no tool uses."""

from __future__ import annotations

import json
import re
from pathlib import Path

from aisquare_pipe_slack import BOT_SCOPES, USER_SCOPES
from aisquare_pipe_slack.constants import TOKEN_ROTATION
from aisquare_pipe_slack.tools import TOOLS

MANIFEST = json.loads((Path(__file__).parent.parent / "slack-app-manifest.json").read_text())
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
