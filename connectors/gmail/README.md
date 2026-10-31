# aisquare-pipe-gmail

Gmail for [aisquare.pipe](../../README.md), over the Gmail v1 REST API (plain
`requests`, no Google SDK). The package ships one thing:

| What | Registered as | Use it for |
|---|---|---|
| `GmailToolkit` | toolkit `gmail` (`aisquare_pipe.toolkits`) | 13 LLM-callable tools — search and read mail and threads, attachments, labels, profile; drafts, send, reply; labels, mark read, trash — plus Google's OAuth spec |

## Install

```bash
pip install aisquare-pipe-gmail              # or: pip install "aisquare-pipe[full]"
cd connectors/gmail && pip install -e ".[dev]"   # development
```

## Connecting (OAuth)

`GMAIL_OAUTH` (also `GmailToolkit.oauth`) describes a Google OAuth 2.0 *web
application* client:

| | |
|---|---|
| authorize | `https://accounts.google.com/o/oauth2/v2/auth` with `access_type=offline`, `prompt=consent` |
| token | `https://oauth2.googleapis.com/token` |
| revoke | `https://oauth2.googleapis.com/revoke` |
| PKCE | required (S256) |
| tokens | access tokens expire after one hour; the refresh token does not rotate |
| default scope | `https://www.googleapis.com/auth/gmail.readonly` |

**Least privilege.** The default consent covers the read tools only. A host
that turns on writing requests the extra scope each tool needs together with
`gmail.readonly` (`scopes=[...]` to `build_authorize_url`). Every tool lists
its scopes in `REQUIRED_SCOPES[slug]` and at the end of its description:

| Scope | Google class | Tools |
|---|---|---|
| `gmail.readonly` | **restricted** | every read tool; also `GMAIL_REPLY_TO_THREAD` (reads the thread it answers) |
| `gmail.compose` | **restricted** | `GMAIL_CREATE_EMAIL_DRAFT` |
| `gmail.send` | sensitive | `GMAIL_SEND_EMAIL`, `GMAIL_REPLY_TO_THREAD` |
| `gmail.modify` | **restricted** | `GMAIL_ADD_LABEL_TO_EMAIL`, `GMAIL_MODIFY_THREAD_LABELS`, `GMAIL_MOVE_TO_TRASH` |

No tool needs `https://mail.google.com/` (full access, permanent delete).

**Google verification.** Gmail's scopes are Google **restricted** scopes
(`gmail.send` is sensitive). A production app that requests them must pass
Google's OAuth app verification *and* an annual third-party security
assessment (CASA). Until then the consent screen shows an "unverified app"
warning, and the app is limited to 100 test users.

**One OAuth client for Drive, Gmail and Calendar.** Each Google toolkit gets
its own token and consent, and one OAuth client may serve all three. That is
why this spec — unlike Google Drive's — does not set
`include_granted_scopes=true`: with it, a Gmail token would also carry every
Drive or Calendar scope the user granted the same client. Two caveats remain
for a host that shares the client:

- **Revoking is grant-wide.** Google's revoke endpoint ends the user's whole
  grant to the OAuth client, so `revoke_token` on a Gmail token also ends that
  user's Drive and Calendar connections. On disconnecting one Google
  connector, delete its stored tokens; revoke only when it is the user's last
  Google connection.
- **The consent screen names the app, not the connector.** Brand the client
  for all three.

Set up: Google Cloud console → APIs & Services → enable the **Gmail API** →
OAuth consent screen (add the Gmail scopes) → Credentials → *OAuth client ID*
(Web application) with your callback as an authorized redirect URI.

## Toolkit

```python
from aisquare_pipe_gmail import GmailToolkit

gmail = GmailToolkit()
config = {"access_token": "ya29...."}

gmail.search_tools("show my unread email", limit=3)          # -> [ToolSpec, ...]
result = gmail.call_tool("GMAIL_FETCH_EMAILS", {"query": "is:unread", "max_results": 5}, config)
result.successful, result.data, result.error, result.error_code
```

`call_tool` never raises. Slugs and argument names are Composio's Gmail
toolkit's, verbatim. The other casing (`messageId`) and Composio's aliases
(`to` for `recipient_email`, `message_body` / `body`) are accepted too.
`user_id` (default `me`) may name another mailbox the token may act for.

**Messages.** A message comes back in Composio's shape: `messageId`,
`threadId`, `labelIds`, `messageTimestamp` (RFC 3339, UTC), `subject`,
`sender`, `to` (`cc`, `date` when present), `preview` (`subject`, `body` —
Gmail's snippet), `messageText` and `attachmentList`. `messageText` is the
`text/plain` body, or the `text/html` body as text when there is no plain
part (scripts and styles dropped), in the part's charset, capped at 20,000
characters (`messageTextTruncated: true` when cut). `attachmentList` holds
every part with a filename — `filename`, `mimeType`, `size`, `attachmentId`,
`partId`, and `inline: true` for inline images. `include_payload` (on
`GMAIL_FETCH_EMAILS`; always on `GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID`) adds
Gmail's raw MIME `payload` tree.

**Lists.** One page per call, hints first:

```python
{"has_more": True, "next_page_token": "…", "result_size_estimate": 201, "items": [...]}
```

Pass `next_page_token` back as `page_token`. Gmail lists ids only, so
`GMAIL_FETCH_EMAILS` reads each message with one more request (as does
`GMAIL_LIST_THREADS` with `verbose`); a page of those is capped at 100.
`ids_only` skips the reads (up to 500 per page) and `verbose=false` reads
headers only. A message deleted between the list and its read is skipped.

**Attachments.** `GMAIL_GET_ATTACHMENT` puts the bytes in `ToolResult.files`
(one `DataEnvelope`: `content_type` = MIME type from `file_name`,
`metadata["filename"]`, `message_id`, `attachment_id`) and describes the file
in `data` (`name`, `mimetype`, `size`, plus `content` for UTF-8 text up to
100 KB) — the shape Google Drive downloads use. `file_name` loses any
directory part. Attachment ids come from `attachmentList`; Gmail may hand out
a different id for the same attachment on each read, and every one works.

**Composing.** `GMAIL_SEND_EMAIL`, `GMAIL_CREATE_EMAIL_DRAFT` and
`GMAIL_REPLY_TO_THREAD` build an RFC 2822 message with the standard
library's `email` package. Recipients may be `a@x.co`, `Ann <a@x.co>` or
comma-separated lists; anything else is refused, as is a header value with a
line break. Non-ASCII subjects and names are RFC 2047-encoded. `is_html`
sends HTML with a plain-text alternative. `attachment` is
`{name, mimetype, file_content_bytes}` (base64) or a list of them, 25 MB in
total; Composio's `s3key` is not supported. Messages up to 4 MB go as JSON
`raw`; larger ones as one `multipart/related` upload (Gmail's limit: 35 MB).

**Replies.** `GMAIL_REPLY_TO_THREAD` (and a draft with `thread_id`) reads the
thread's headers first. The reply goes to the last message's `Reply-To` or
`From` — or, when the user sent that message, to its `To` — unless
`recipient_email`/`cc`/`bcc` are given. It carries `Re: <subject>`,
`In-Reply-To` and `References`, so it threads for every recipient.

**Labels.** `add_label_ids` / `remove_label_ids` take system label ids in
any case (`unread` → `UNREAD`), user label ids (`Label_12`), or a user
label's exact name (one `labels.list` lookup), as a list, a comma-separated
string or a JSON array. Mark read = remove `UNREAD`; archive = remove
`INBOX`.

**Errors.** A failed call carries core's canonical `error_code`, a
human-readable `error` (`"<status>: <Google's message>"` plus a hint),
`retry_after` (seconds) when rate limited, and `data={"status", "reason"}`
when Google answered:

| Failure | `error_code` | Notes |
|---|---|---|
| 401 | `unauthorized` | refresh the token, or reconnect |
| 403 for a missing scope (`insufficientPermissions`, `ACCESS_TOKEN_SCOPE_INSUFFICIENT`, or `WWW-Authenticate: … insufficient_scope`) | `insufficient_scope` once core defines it, `forbidden` until then | `data` adds `insufficient_scope: true` and the tool's `required_scopes` |
| 429, or a 403 `rateLimitExceeded` / `userRateLimitExceeded`, once retries or the budget run out | `rate_limited` (+ `retry_after`) | |
| other 403 (delegation denied, daily limit, domain policy) | `forbidden` | |
| 404, 410 | `not_found` | |
| 400, and arguments the toolkit refuses | `invalid_arguments` | |
| request timeout, 504, spent `timeout_seconds` | `timeout` | |
| 5xx, network failure | `upstream_error` | |

**Retries.** Rate limits are retried up to 3 times (1 s, 2 s, 4 s, or a finite
`Retry-After`) on any method, since Google did not process the request. A
500/502/503/504 is retried on GETs only: a send that failed half-way is never
repeated. Total sleep per call is capped at 60 s.

**Time budget.** `config["timeout_seconds"]` bounds the whole call: each
request's timeout is the smaller of 30 s (120 s for attachments and large
sends) and the time left, a retry that would outlast it fails at once, and a
budget spent between the reads of a page fails as `timeout`.

**Concurrency and secrets.** A `GmailToolkit` holds no mutable state; each
call opens and closes its own HTTP session (`session=` opts into a shared
one). The token rides the `Authorization` header only — never a URL, log
line, error or repr. Logs carry method, status and timing, never paths,
queries or addresses.

### Tools

Read-only (`gmail.readonly`):

| Slug | Gmail endpoint | Use for |
|---|---|---|
| `GMAIL_FETCH_EMAILS` | `GET messages` + `GET messages/{id}` | search or list mail |
| `GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID` | `GET messages/{id}` | one message (`format`: full, metadata, minimal, raw as a `.eml` file) |
| `GMAIL_LIST_THREADS` | `GET threads` | list or search conversations |
| `GMAIL_FETCH_MESSAGE_BY_THREAD_ID` | `GET threads/{id}` | a whole conversation |
| `GMAIL_GET_ATTACHMENT` | `GET messages/{id}/attachments/{id}` | an attachment's bytes (`ToolResult.files`) |
| `GMAIL_LIST_LABELS` | `GET labels` | label ids and names (`include_details`: counts) |
| `GMAIL_GET_PROFILE` | `GET profile` | the account's address and totals |

Writes:

| Slug | Gmail endpoint | Scope | Notes |
|---|---|---|---|
| `GMAIL_CREATE_EMAIL_DRAFT` | `POST drafts` | `gmail.compose` | `thread_id` makes it a reply draft |
| `GMAIL_SEND_EMAIL` | `POST messages/send` | `gmail.send` | `from_email`: a send-as alias |
| `GMAIL_REPLY_TO_THREAD` | `GET threads/{id}` + `POST messages/send` | `gmail.readonly` + `gmail.send` | |
| `GMAIL_ADD_LABEL_TO_EMAIL` | `POST messages/{id}/modify` | `gmail.modify` | mark read/unread, star, archive |
| `GMAIL_MODIFY_THREAD_LABELS` | `POST threads/{id}/modify` | `gmail.modify` | the same for a conversation |
| `GMAIL_MOVE_TO_TRASH` | `POST messages/{id}/trash` | `gmail.modify` | **destructive**; restorable from Trash for 30 days |

## Tests

```bash
cd connectors/gmail && pytest            # hermetic: faked HTTP, socket guard on
pytest -m live tests/test_live.py        # opt-in, read-only; needs GMAIL_ACCESS_TOKEN
```
