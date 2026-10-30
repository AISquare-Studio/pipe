"""Names and limits shared across aisquare-pipe-slack."""

from __future__ import annotations

API_BASE = "https://slack.com/api"
USER_AGENT = "aisquare-pipe-slack"

# -- identity ---------------------------------------------------------------

#: Act as the person who connected (a user token, ``xoxp-…``).
IDENTITY_USER = "user"
#: Act as the app's bot user (a bot token, ``xoxb-…``).
IDENTITY_BOT = "bot"
IDENTITIES = (IDENTITY_USER, IDENTITY_BOT)

# -- risk tiers ---------------------------------------------------------------

#: No side effects.
TIER_READ = "read"
#: Posts into a conversation: a message, reply, DM (opening the DM included),
#: ephemeral, scheduled or /me message, or a file uploaded into chosen
#: conversations.
TIER_SEND = "send"
#: Changes state but deletes nothing: edits, reactions, pins, channel admin,
#: profile and presence.
TIER_WRITE = "write"
#: Deletes, archives, removes people, or otherwise can't be undone by the caller.
TIER_DESTRUCTIVE = "destructive"
TIERS = (TIER_READ, TIER_SEND, TIER_WRITE, TIER_DESTRUCTIVE)
#: What a connection may call when its config names no tiers, as the user.
DEFAULT_TIERS = (TIER_READ, TIER_SEND)
#: ... and as the bot: send only. A bot token is shared by the workspace, so
#: reading as the bot would show one person every conversation the bot is in
#: (other people's DMs with it, private channels others added it to).
DEFAULT_BOT_TIERS = (TIER_SEND,)

# -- config keys (non-secret; set by the host next to access_token) ----------

CONFIG_IDENTITY = "identity"
CONFIG_ENABLED_TIERS = "enabled_tiers"
CONFIG_ENABLED_TOOLS = "enabled_tools"
CONFIG_ATTRIBUTION_NAME = "attribution_name"
CONFIG_BOT_ATTRIBUTION = "bot_attribution"

#: The ``<name>`` in "Sent by <name>" when the config sets none.
DEFAULT_ATTRIBUTION_NAME = "pipe"
#: Longest attribution name used; a longer one is cut.
MAX_ATTRIBUTION_NAME = 80
ATTRIBUTION_TEMPLATE = "Sent by {name}"

# -- token rotation ---------------------------------------------------------

#: Prefixes of a bot token (plain, and with rotation on). Used only to fail
#: safe: the attribution is dropped only for a token that is positively a
#: bot's. Tokens are never *validated* by prefix — Slack has changed them.
BOT_TOKEN_PREFIXES = ("xoxb-", "xoxe.xoxb-")

#: Mirrors the app manifest's ``settings.token_rotation_enabled`` (off for v1).
#: The OAuth spec's expiry hints follow it; nothing depends on rotation.
TOKEN_ROTATION = False

# -- result sizes -------------------------------------------------------------

#: Longest JSON rendering of a message list a result carries; past it the list
#: is cut and a resume hint set. pipe-service caps a whole result near this.
RESULT_CHAR_CAP = 100_000
#: conversations.history / conversations.replies page size (default, max).
HISTORY_DEFAULT_LIMIT = 50
HISTORY_MAX_LIMIT = 200
#: Other cursor lists (default, max).
LIST_DEFAULT_LIMIT = 100
LIST_MAX_LIMIT = 999  # conversations.list / users.list: "under 1000"; users.conversations: 999
#: search.* page size (default, max).
SEARCH_DEFAULT_COUNT = 20
SEARCH_MAX_COUNT = 100
#: Page size and page cap for composites that walk a list (channel name
#: lookup, FIND_CHANNELS, FIND_USERS).
SCAN_PAGE_SIZE = 200
SCAN_MAX_PAGES = 10
#: Slack's own cap on blocks in one message.
MAX_BLOCKS = 50
#: Slack's cap on ``markdown_text`` (characters), attribution included.
MAX_MARKDOWN_TEXT = 12_000
