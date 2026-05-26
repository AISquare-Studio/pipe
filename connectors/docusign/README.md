# aisquare-pipe-docusign

DocuSign source and sink connectors for [aisquare.pipe](https://github.com/AISquare-Studio/pipe). Ships with the `aisquare-pipe[full]` extras bundle.

## Install

```bash
pip install aisquare-pipe-docusign
```

`aisquare-pipe` is pulled in transitively — no separate install needed.

For development:

```bash
cd connectors/docusign
pip install -e ".[dev]"
```

## Configuration

Two authentication flows are supported. The connector picks the right one based on which keys are present in `config`.

```python
# Flow 1: JWT Grant (recommended for server-to-server / platform integrations)
config = {
    "integration_key": "YOUR_INTEGRATION_KEY",
    "user_id":         "USER_GUID_TO_IMPERSONATE",
    "private_key":     "-----BEGIN RSA PRIVATE KEY-----\n...",  # str or bytes
    "auth_server":     "account-d.docusign.com",  # "account.docusign.com" for prod
    "account_id":      "optional — auto-discovered if omitted",
}

# Flow 2: Authorization Code refresh-token (user OAuth)
config = {
    "client_id":     "INTEGRATION_KEY",
    "client_secret": "SECRET_KEY",
    "refresh_token": "REFRESH_TOKEN",
    "auth_server":   "account-d.docusign.com",
    "account_id":    "optional",
}
```

> **JWT consent**: the first time JWT Grant is used for a given integration key + user, a one-time consent URL must be visited manually. See DocuSign's [JWT Grant Authentication](https://developers.docusign.com/platform/auth/jwt/) guide.

## Usage

### Pull signed documents (default mode)

```python
from aisquare.pipe import PullParams
from aisquare_pipe_docusign import DocusignSource

source = DocusignSource()
params = PullParams(params={
    "status": "completed",
    "from_date": "2024-01-01",
    "limit": 50,
})

for envelope in source.pull(config, params):
    # envelope.data is PDF bytes; metadata has envelope_id, document_id, filename, status
    print(envelope.metadata["filename"], len(envelope.data), "bytes")
```

### Pull envelope metadata instead

```python
params = PullParams(params={
    "mode": "envelopes",
    "status": "completed",
    "from_date": "2024-01-01",
})

for envelope in source.pull(config, params):
    # envelope.data is a dict of envelope fields (status, subject, recipients, ...)
    print(envelope.metadata["envelope_id"], envelope.data["status"])
```

### Send a document for signature

```python
from aisquare.pipe import DataEnvelope
from aisquare_pipe_docusign import DocusignSink

sink = DocusignSink()
with open("contract.pdf", "rb") as f:
    pdf_bytes = f.read()

envelope = DataEnvelope(
    content_type="application/pdf",
    data=pdf_bytes,
    source_id="my-app",
    metadata={
        "filename": "contract.pdf",
        "signers": [
            {"name": "Alice Example", "email": "alice@example.com"},
            {"name": "Bob Example",   "email": "bob@example.com"},
        ],
        "subject": "Please sign this contract",
        "email_blurb": "Sign at your convenience.",
    },
)
result = sink.push(envelope, config)
print(f"Sent envelope: {result.ref}")
```

## Pull params reference

| Key | Default | Meaning |
|---|---|---|
| `mode` | `"documents"` | `"documents"` → one envelope per signed PDF; `"envelopes"` → one envelope per signing transaction as JSON |
| `status` | (none) | Filter — `"completed"`, `"sent"`, `"delivered"`, ... |
| `from_date` | (none) | ISO date; DocuSign requires at least one of `from_date`/`envelope_ids` |
| `to_date` | (none) | ISO date |
| `folder_id` | (none) | Restrict to a folder |
| `envelope_ids` | (none) | List of specific envelope IDs; skips listing |
| `include_combined` | `False` | Documents mode: also fetch the combined PDF (document_id=`"combined"`) |
| `limit` | (none) | Cap total envelopes |
| `account_id` | (auto) | Override account |

## Push metadata reference

| Key | Required | Meaning |
|---|---|---|
| `filename` | yes | Document filename shown to signers |
| `signers` | yes | List of `{name, email, recipient_id?, routing_order?}` dicts |
| `subject` | no | Email subject (default: `"Please sign"`) |
| `email_blurb` | no | Email body |

## Features

- **Two auth flows** — JWT Grant (server-to-server) and Authorization Code refresh-token (user OAuth)
- **Two source modes** — pull signed PDFs or envelope metadata via `params["mode"]`
- **Status / date / folder / envelope-id filters** — translated into the `list_status_changes` call
- **Rate-limit aware** — exponential backoff on HTTP 429 / 503
- **Auto-discovery of account_id** — supplied by `get_user_info`, overridable via `config["account_id"]`
