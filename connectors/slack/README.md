# aisquare-pipe-slack

Slack for [aisquare.pipe](../../README.md): `SlackToolkit`, registered as toolkit
`slack` (`aisquare_pipe.toolkits`). It has 51 LLM-callable tools over Slack's Web API —
messages, history and threads, search, channels and DMs, reactions, pins, people and
presence — and Slack's OAuth v2 spec. It acts **as the user** who connected, or
**as the app's bot**. Which one is a config option.

pipe is app-agnostic. The host registers one Slack app and passes its client ID and
secret, and its choices, into pipe. Which app that is (prod, staging, …) is the
host's business.

## Install

```bash
cd connectors/slack
pip install -e ".[dev]"
```

## Quick start

```python
from aisquare_pipe_slack import SlackToolkit

slack = SlackToolkit()
config = {"access_token": "xoxp-…"}          # the user's token; see Config below

slack.available_tools(config)                 # what this connection may call
result = slack.call_tool(
    "SLACK_SEND_MESSAGE", {"channel": "#general", "markdown_text": "Standup in 5"}, config
)
result.successful, result.data, result.error, result.error_code
```

`call_tool` never raises. Slugs and argument names follow Composio's Slack toolkit
word for word (frozen in `tests/fixtures/composio_slack_args.json`; the few arguments
left out are listed with reasons in `tests/test_compat_composio.py`).

## Config

Everything except `access_token` is optional and non-secret. The defaults are the
safe choices.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `access_token` | string | — (required) | The token for `identity`: a user token (`xoxp-…`) or the app's bot token (`xoxb-…`). Never put it anywhere else. |
| `identity` | `"user"` \| `"bot"` | `"user"` | `user`: act as the person who connected, with their access, under their name. `bot`: post as the app's bot user. |
| `enabled_tiers` | list of tier names | user: `["read", "send"]`; bot: `["send"]` | The risk tiers this connection may call. It **replaces** the default, so list every tier you want. A comma-separated string works too. There is no wildcard. **Read under the bot exposes other people's conversations** (see Identity). |
| `enabled_tools` | list of slugs | `[]` | Extra tools enabled one at a time, whatever their tier (e.g. `["SLACK_ADD_REACTION_TO_AN_ITEM"]`). |
| `attribution_name` | string | `"pipe"` | The `<name>` in the "Sent by <name>" line (e.g. `"aisquare"` or `"pipe by aisquare"`). Whitespace is collapsed, and it is cut at 80 characters. |
| `bot_attribution` | bool | `false` | Also add the line to messages the bot sends. The line is always added for the user identity. |
| `timeout_seconds` | number | none | Set by hosts. It bounds the whole call, including retries and rate-limit sleeps. |

A value the toolkit can't use (an unknown tier such as `"destrucive"`, an identity
of `"app"`, …) fails every call with `forbidden`, and the message names the key.
The toolkit never guesses what you meant. `forbidden` was chosen over
`upstream_error`, because a host may retry the latter and this is the host's
misconfiguration, not a Slack failure. It was chosen over `invalid_arguments`
because the model can't fix it by changing its arguments.

### Risk tiers

Every tool has exactly one tier:

| Tier | What it covers | On by default |
|---|---|:-:|
| `read` | No side effects | ✓ |
| `send` | Posting a new message: post, reply, DM (incl. opening the DM), ephemeral, scheduled | ✓ |
| `write` | Changes that delete nothing: editing a message, reactions, pins, channel admin (create, join, invite, rename, topic, purpose, unarchive), closing a DM, profile/status, presence | |
| `destructive` | Things the caller can't undo: deleting a message or a scheduled message, archiving a channel, removing someone from a channel, leaving a channel (a private one can only be re-entered by invitation) | |

If a connection calls a tool whose tier it hasn't enabled, the call fails with
`forbidden` **before any request is made**. The message names the setting that
would allow it:

> `SLACK_ARCHIVE_CONVERSATION is a 'destructive' tool and is not enabled for this Slack connection: the host enables it by adding 'destructive' to config['enabled_tiers'] or 'SLACK_ARCHIVE_CONVERSATION' to config['enabled_tools']`

To show a model only what it may call, **list and search through the connection's
config**:

- `available_tools(config)` is core's config-aware listing hook.
- `search_tools(query, limit, config=config)` ranks `available_tools(config)`.

Without a config, both give the **default exposure: read + send only**. A host that
lists without passing the connection's config therefore never offers a write or
destructive tool. `search_tools` is defined on `SlackToolkit` too, so this also holds
on an older core. Only `tools()` returns the full catalog, for browsing.
`SlackToolkit.tier_of(slug)` gives a tool's tier.

`ToolSpec.read_only` is true exactly for the `read` tier, and `destructive` is true
for the `destructive` tier. There is one exception:
`SLACK_UPDATES_A_SLACK_MESSAGE` is also flagged `destructive`, because an edit
overwrites the message. It still sits in the `write` tier, because the owner put
edits there.

### Identity

| | `identity: "user"` | `identity: "bot"` |
|---|---|---|
| Token | user token (`xoxp-…`) | bot token (`xoxb-…`) |
| Posts appear as | the user, with "Sent by <name>" | the app's bot (marker only with `bot_attribution`) |
| Default tiers | read + send | **send only** |
| Can read (with `read` enabled) | what that user can see | **every conversation the bot is in, whoever they belong to** |
| User-only tools | all available | `SLACK_SEARCH_*`, `SLACK_LIST_UNREAD_CHANNEL_MESSAGES`, `SLACK_SET_USER_PROFILE`, `SLACK_UNARCHIVE_CHANNEL` fail as `forbidden` without calling Slack (Slack accepts only user tokens for them). `available_tools` leaves them out. |
| Group DMs | ✓ | DMs only (the bot does not request `mpim:write`) |

> **Warning: reading as the bot crosses users.** One bot token serves the whole
> workspace, and a host typically stores it once per team. With `read` enabled
> under `identity: "bot"`, anyone given that connection can read every
> conversation the bot is in. That includes other people's DMs with the bot, and
> private channels that other people added it to. This is why `read` is off by
> default for the bot. Enable it only for workspace admins or the person who
> installed the app.

`SLACK_OPEN_DM` is a send step, so it returns only what sending needs: the channel ID
(plus `is_im` / `user` and `already_open` / `no_op`). This holds for every identity
and tier. Composio's `return_im` is not accepted, because the full DM object carries
the latest message and read state.

`identity` comes from config. The token's own kind isn't checked, because Slack has
changed token prefixes before. Where the two can disagree, the toolkit fails safe:
the "Sent by" marker is dropped only when the token is positively a bot token
(`xoxb-…`, `xoxe.xoxb-…`). A user token configured as `bot` still marks its
messages, since they still appear under the user's name.

### Attribution

Messages sent with the user identity show a visible "Sent by <name>" line, like
Claude's "Sent using Claude". This covers `SEND_MESSAGE`, `SEND_EPHEMERAL_MESSAGE`,
`SCHEDULE_MESSAGE` and edits through `UPDATES_A_SLACK_MESSAGE`. How the line is
added depends on the content:

- **`blocks`:** a context block (small grey text) goes after the blocks. This leaves
  room for at most 49 blocks of your own.
- **`markdown_text`:** a final italic line, `_Sent by pipe_`. Slack won't combine
  `markdown_text` with blocks, so a context block isn't possible here.
- **plain `text`:** a final italic line.

The name is escaped for each format:

- **mrkdwn** (`text`, context blocks): `&`, `<` and `>` become entities.
- **`markdown_text`**: only the characters markdown would read as markup get a
  backslash: `` \ ` * _ ~ [ ] < > ``. A typical name such as `pipe.ai (R&D)` gets
  none, so it shows as written either way. Slack doesn't document whether
  `markdown_text` honours CommonMark escapes; `tests/test_live.py` checks the posted
  message for stray backslashes. Slack caps `markdown_text` at 12,000
characters including the marker line, so the toolkit refuses longer content with
`invalid_arguments` before sending. The error says how much room is left.

## Results

You get Slack's payload without `ok`, `response_metadata` or `warning`. Paged reads
put their hints **first**, so they survive a caller truncating the result:

```python
{"has_more": True, "next_cursor": "dXNlcjpVMDYx…", "messages": [...]}  # cursor methods
{"has_more": True, "next_page": 2, "messages": {"matches": [...]}}    # search.*
```

- **`has_more`** comes from `next_cursor`, never from the page size. Slack pages can
  come back short in the middle of a collection.
- **Message lists are slimmed.** The `rich_text` mirror of `text`, `user_profile`
  and bot icons are dropped.
- **Long message lists are cut** at 100,000 characters. When that happens, the
  result has `truncated: true` and a resume hint: `resume_latest` for history,
  `resume_oldest` for threads.
- **Channel arguments** take an ID (`C…`, `G…`, `D…`, or a user ID `U…` for
  `chat.postMessage`) or a name (`general` or `#general`). A name is looked up with
  `conversations.list`, which costs an extra call; IDs avoid that. If a name is
  ambiguous (on Enterprise Grid, two workspaces can each have `#general`), the call
  is **refused** with `invalid_arguments`, never guessed.
  - The same applies when the lookup stops at its cap (2,000 channels) with more
    left: one match so far isn't proof there's no second one.
  - An ID-shaped value containing a digit (`C04PRODUCT`) is used as an ID straight
    away. An all-caps value without one (`GENERAL`, `CABCDEFGH`) is tried as a name
    first, and used as an ID if no channel has that name, since channel names are
    never uppercase. Slack doesn't promise IDs contain a digit: Slackbot is
    `USLACKBOT`. `SLACK_FIND_USERS` does the same with `users.info`.

## Errors

Slack reports almost every failure as **HTTP 200 with `{"ok": false, "error":
"<code>"}`**. Only rate limits are guaranteed to use HTTP 429. The client folds the
HTTP status and the `ok` field into one error type, and `error_code` is decided in
this order:

1. **Transport.** A client timeout is `timeout`; a network failure is `upstream_error`.
2. **HTTP status.** 429 is `rate_limited`; 408/504 is `timeout`; any other 5xx is
   `upstream_error`; any other non-2xx goes through core's `error_code_for_status`.
3. **Slack's code.** For example: `invalid_auth`/`token_revoked` are `unauthorized`;
   `missing_scope`/`not_in_channel`/`not_allowed_token_type` are `forbidden`;
   `*_not_found` is `not_found`; `already_reacted`/`is_archived` are `conflict`;
   `invalid_cursor`/`msg_too_long` are `invalid_arguments`; unknown codes are
   `upstream_error`. The full table is in `toolkit.py`.

`conflict` reads as **"already in that state; no change needed"** only where the
change really is in place: `already_reacted`, `no_reaction`, `already_pinned`,
`not_pinned`, `already_archived`, `not_archived` and `already_in_channel`. Other
conflicts mean nothing happened, and each has its own hint:

- `is_archived` / `is_inactive`: the channel is archived or frozen;
- `name_taken`: pick another name;
- `too_many_pins` / `too_many_reactions`: Slack's limit.

Workspace settings and plan limits (`edit_window_closed`, `message_limit_exceeded`,
`messages_tab_disabled`) are `forbidden`. `SLACK_API_TEST` reports an echoed
`error` as `upstream_error` and never retries, so a model can't make a healthy
connection look revoked or rate limited.

`missing_scope` is `forbidden`, **not** `unauthorized`. Its message says
"reconnect Slack to grant `<scope>`", so a host doesn't loop through refreshes.
Messages look like `"<method>: <slack code> — <what to do>"`, and `data` is
`{"slack_error": code, "status": http_status}`. Neither ever carries arguments or
the token. Slack's code is passed on only if it looks like one (`[a-z0-9_]+`, and
the same for `needed` scopes); anything else is reported as `unknown_error`.

A `not_in_channel` hint points to `SLACK_JOIN_AN_EXISTING_CONVERSATION` only when
that write-tier tool is enabled for the connection. Otherwise the hint says to ask a
member for an invite, and names the config keys that would enable joining.

**Rate limits.** A 429 (or `ok: false, error: ratelimited`) is retried up to twice
for up to 60 s in total, within `timeout_seconds`.

- `Retry-After` is parsed defensively. A finite value of 0 or more is used and capped
  at 3600 s; anything else counts as 60 s.
- When the wait wouldn't leave time for the retried request, the call fails at once
  as `rate_limited`, with `retry_after`.
- Retrying a 429 is safe for writes too, because Slack didn't process the request.
- Server errors are retried only for reads. Retrying a post could send it twice.

**History limit for apps outside the Marketplace.** Since 2025-05-29, Slack limits
`conversations.history` and `conversations.replies` to **1 call a minute and 15
messages a page** for new installs of commercially distributed apps that aren't in
the Marketplace. The tool descriptions steer models toward `SLACK_SEARCH_MESSAGES`,
or toward one narrow page.

**Unread messages.** `SLACK_LIST_UNREAD_CHANNEL_MESSAGES` relies on the
`last_read` field from `conversations.info`, and Slack documents that field for DMs.
If a conversation doesn't report it, the tool fails with `invalid_arguments` and
points to `SLACK_FETCH_CONVERSATION_HISTORY`. It does not use `upstream_error`,
because retrying won't help. The live test checks a channel and an optional DM.

## Scopes

Every tool declares the scopes it needs for each identity, so the scope lists are
derived from the catalog rather than kept by hand. `USER_SCOPES` (29) and
`BOT_SCOPES` (28) are the full unions, and they are what a consent asks for by
default: one consent screen, and no re-consent later, because Slack scopes only
ever add up.

To ask for less, pass `scopes` to `build_authorize_url`. It takes **scope group**
names, raw Slack scopes, or both:

- By area: `messages`, `history`, `search`, `conversations`, `dms`, `reactions`,
  `pins`, `users`, `workspace`.
- By tier: `read`, `send`, `write`, `destructive`. For example, `["read", "send"]`
  asks for exactly what the default tiers need.

```python
from aisquare_pipe_slack import SCOPE_GROUPS, scopes_for

scopes_for(["read", "send"], "user")       # -> (user_scopes, ())
scopes_for(["messages", "pins:read"], "both")
```

An unknown group, or a raw scope that no tool of that identity uses, raises
`OAuthError(error="invalid_scope")`. It isn't sent to Slack, where it would fail the
whole consent screen.

## Connecting (OAuth)

`SLACK_OAUTH` is Slack's OAuth v2:

- authorize at `https://slack.com/oauth/v2/authorize`, exchange at `oauth.v2.access`;
- comma-separated scopes;
- PKCE off: turning it on in Slack makes the app a public client permanently, and caps
  refresh tokens at 30 days;
- token rotation off.

One consent can mint a user token, a bot token, or both. **The default is both, with
every scope.** That makes it one consent screen: switching `config["identity"]`
later needs no re-consent.

```python
url = slack.build_authorize_url(
    client_id=…, redirect_uri=…, state=csrf, scopes=None
)  # identity: "both" (default) | "user" | "bot"; user scopes go in user_scope, bot scopes in scope

token = slack.exchange_code(client_id=…, client_secret=…, code=…, redirect_uri=…)
# -> core's OAuthToken: the user token, or the bot token when no user token was minted;
#    identity="bot" picks the bot token

installation = slack.exchange_installation(client_id=…, client_secret=…, code=…, redirect_uri=…)
# -> SlackInstallation(user_token, bot_token, team_id, enterprise_id, user_id, bot_user_id, app_id)
```

- **`refresh_access_token`** is only used if the app turns on token rotation. Slack's
  `invalid_refresh_token` comes back as `OAuthError(error="invalid_grant")`, a dead
  grant.
- **`revoke_token`** revokes **one user token** with `auth.revoke`. It never revokes
  a bot token: `auth.test` identifies one, and it returns `False` without revoking,
  because the bot token is shared by every connection in that workspace. It never
  calls `apps.uninstall`.
- **Whether to revoke on disconnect is the host's decision.** If Slack gives the same
  user token to two connections of one person, revoking it from one breaks the other.

## The Slack app

[`slack-app-manifest.json`](slack-app-manifest.json) is an app manifest template:
user and bot scopes, the bot user, PKCE off, token rotation off. pipe is
app-agnostic, so first replace the placeholders `YOUR_APP_NAME`, `your-app-bot` and
`https://YOUR_HOST/v1/connections/callback` with your app's values. Then go to
<https://api.slack.com/apps?new_app=1>, choose **From a manifest**, and use the JSON
tab.
`tests/test_manifest.py` keeps its scopes equal to what the tools declare. It also
checks every declared scope against Slack's own method docs, frozen in
`tests/fixtures/slack_method_scopes.json`.

Other workspaces can't install the app until you turn on **Manage Distribution →
Activate Public Distribution**. Until then, installs fail with
`invalid_team_for_non_distributed_app`.

## Tools

| Tool | Tier | Slack method(s) | Identity |
|---|---|---|---|
| **messages** | | | |
| `SLACK_SEND_MESSAGE` | send | `chat.postMessage` | user, bot |
| `SLACK_UPDATES_A_SLACK_MESSAGE` | write | `chat.update` | user, bot |
| `SLACK_DELETES_A_MESSAGE_FROM_A_CHAT` | destructive | `chat.delete` | user, bot |
| `SLACK_SEND_EPHEMERAL_MESSAGE` | send | `chat.postEphemeral` | user, bot |
| `SLACK_SCHEDULE_MESSAGE` | send | `chat.scheduleMessage` | user, bot |
| `SLACK_LIST_SCHEDULED_MESSAGES` | read | `chat.scheduledMessages.list` | user, bot |
| `SLACK_DELETE_SCHEDULED_MESSAGE` | destructive | `chat.deleteScheduledMessage` | user, bot |
| `SLACK_RETRIEVE_MESSAGE_PERMALINK_URL` | read | `chat.getPermalink` | user, bot |
| **history** | | | |
| `SLACK_FETCH_CONVERSATION_HISTORY` | read | `conversations.history` | user, bot |
| `SLACK_FETCH_MESSAGE_THREAD_FROM_A_CONVERSATION` | read | `conversations.replies` | user, bot |
| `SLACK_LIST_UNREAD_CHANNEL_MESSAGES` | read | `conversations.info`, `conversations.history` | user |
| **search** | | | |
| `SLACK_SEARCH_MESSAGES` | read | `search.messages` | user |
| `SLACK_SEARCH_FILES` | read | `search.files` | user |
| `SLACK_SEARCH_ALL` | read | `search.all` | user |
| `SLACK_FIND_CHANNELS` | read | `conversations.list` | user, bot |
| `SLACK_FIND_USERS` | read | `users.info`, `users.lookupByEmail`, `users.list` | user, bot |
| **conversations** | | | |
| `SLACK_LIST_ALL_CHANNELS` | read | `conversations.list` | user, bot |
| `SLACK_LIST_CONVERSATIONS` | read | `users.conversations` | user, bot |
| `SLACK_RETRIEVE_CONVERSATION_INFORMATION` | read | `conversations.info` | user, bot |
| `SLACK_RETRIEVE_CONVERSATION_MEMBERS_LIST` | read | `conversations.members` | user, bot |
| `SLACK_CREATE_CHANNEL` | write | `conversations.create` | user, bot |
| `SLACK_JOIN_AN_EXISTING_CONVERSATION` | write | `conversations.join` | user, bot |
| `SLACK_LEAVE_CONVERSATION` | destructive | `conversations.leave` | user, bot |
| `SLACK_INVITE_USERS_TO_A_SLACK_CHANNEL` | write | `conversations.invite` | user, bot |
| `SLACK_REMOVE_USER_FROM_CONVERSATION` | destructive | `conversations.kick` | user, bot |
| `SLACK_ARCHIVE_CONVERSATION` | destructive | `conversations.archive` | user, bot |
| `SLACK_UNARCHIVE_CHANNEL` | write | `conversations.unarchive` | user |
| `SLACK_RENAME_CONVERSATION` | write | `conversations.rename` | user, bot |
| `SLACK_SET_THE_TOPIC_OF_A_CONVERSATION` | write | `conversations.setTopic` | user, bot |
| `SLACK_SET_CONVERSATION_PURPOSE` | write | `conversations.setPurpose` | user, bot |
| **dms** | | | |
| `SLACK_OPEN_DM` | send | `conversations.open` | user, bot |
| `SLACK_CLOSE_DM` | write | `conversations.close` | user, bot |
| **reactions** | | | |
| `SLACK_ADD_REACTION_TO_AN_ITEM` | write | `reactions.add` | user, bot |
| `SLACK_REMOVE_REACTION_FROM_ITEM` | write | `reactions.remove` | user, bot |
| `SLACK_FETCH_ITEM_REACTIONS` | read | `reactions.get` | user, bot |
| `SLACK_LIST_USER_REACTIONS` | read | `reactions.list` | user, bot |
| **pins** | | | |
| `SLACK_PIN_ITEM` | write | `pins.add` | user, bot |
| `SLACK_UNPIN_ITEM` | write | `pins.remove` | user, bot |
| `SLACK_LIST_PINNED_ITEMS` | read | `pins.list` | user, bot |
| **users** | | | |
| `SLACK_LIST_ALL_USERS` | read | `users.list` | user, bot |
| `SLACK_RETRIEVE_DETAILED_USER_INFORMATION` | read | `users.info` | user, bot |
| `SLACK_FIND_USER_BY_EMAIL_ADDRESS` | read | `users.lookupByEmail` | user, bot |
| `SLACK_RETRIEVE_USER_PROFILE_INFORMATION` | read | `users.profile.get` | user, bot |
| `SLACK_SET_USER_PROFILE` | write | `users.profile.set` | user |
| `SLACK_GET_USER_PRESENCE` | read | `users.getPresence` | user, bot |
| `SLACK_SET_USER_PRESENCE` | write | `users.setPresence` | user, bot |
| **workspace** | | | |
| `SLACK_FETCH_TEAM_INFO` | read | `team.info` | user, bot |
| `SLACK_LIST_CUSTOM_EMOJIS` | read | `emoji.list` | user, bot |
| `SLACK_TEST_AUTH` | read | `auth.test` | user, bot |
| `SLACK_WHO_AM_I` | read | `auth.test`, `team.info` | user, bot |
| `SLACK_API_TEST` | read | `api.test` | user, bot |

## Tests

Every test is hermetic: an autouse guard fails any test that opens a socket. The
live checks are opt-in and post to, then delete from, a dedicated test channel:

```bash
cd connectors/slack
pytest                                   # unit + compliance (no network, no credentials)
SLACK_TEST_USER_TOKEN=xoxp-… SLACK_TEST_CHANNEL=C… [SLACK_TEST_BOT_TOKEN=xoxb-…] pytest -m live
```
