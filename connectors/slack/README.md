# aisquare-pipe-slack

Slack connector for `aisquare.pipe`. Outbound-only in v1.

## Connectors

| Entry point | Class | Direction | Auth |
|---|---|---|---|
| `slack-message-sink` | `SlackMessageSink` | push | OAuth2 bot token (`xoxb-…`) |

## Usage

```python
from aisquare.pipe.core.envelope import DataEnvelope, PushParams
from aisquare_pipe_slack import SlackMessageSink

sink = SlackMessageSink()
result = sink.push(
    DataEnvelope(
        content_type="application/json",
        data={"text": "New studio published!", "blocks": []},
        source_id="studio-be",
    ),
    {"bot_token": "xoxb-..."},
    PushParams({"channel": "C0123456789"}),
)
assert result.success
message_ts = result.ref
```

- **Envelope** carries the message body: a plain string, or a dict with
  `text` and/or Block Kit `blocks`. `metadata.blocks` overrides envelope
  blocks; `metadata.username` / `metadata.icon_emoji` restyle channel posts.
- **Params** carry targeting: `channel` (required), `thread_ts` (reply in a
  thread), `ephemeral_user` (deliver via `chat.postEphemeral`, visible only
  to that user; `result.ref` is then `message_ts` instead of `ts`).
- Slack reports most failures as HTTP 200 + `{"ok": false, "error": …}`;
  the client folds those onto `SlackError` / `SlackAuthError` /
  `SlackRateLimited` (the latter exposes `retry_after` seconds).

## Inbound (host-owned)

`aisquare_pipe_slack.webhook` ships pure helpers for hosts receiving Slack
Events API requests: `verify_slack_signature(raw_body, timestamp, signature,
signing_secret)` (v0 HMAC, constant-time) and `timestamp_in_window(ts)`.
The host owns the HTTP route, secret storage, the `url_verification`
challenge handshake, and event dedup.

## Required Slack app scopes (for the sink)

`chat:write` — plus `chat:write.customize` if you use the
`username`/`icon_emoji` overrides.

## Tests

```bash
cd connectors/slack
pip install -e ".[dev]"
pytest
```
