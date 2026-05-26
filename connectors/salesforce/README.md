# aisquare-pipe-salesforce

Salesforce source and sink connectors for [aisquare.pipe](https://github.com/AISquare-Studio/pipe). Ships with the `aisquare-pipe[full]` extras bundle.

## Install

```bash
pip install aisquare-pipe-salesforce
```

`aisquare-pipe` is pulled in transitively — you don't need to install it separately.

For development (from the addon directory):

```bash
cd connectors/salesforce
pip install -e ".[dev]"
```

## Configuration

Two authentication flows are supported. The connector picks the right one based on which keys are present in `config`.

```python
# Flow 1: Username + password + security token (quick start / sandboxes)
config = {
    "username": "you@example.com",
    "password": "your-password",
    "security_token": "XXXX",
    "domain": "login",       # optional — "test" for sandboxes, default "login"
}

# Flow 2: OAuth2 refresh-token (recommended for production)
config = {
    "client_id":     "CONNECTED_APP_CONSUMER_KEY",
    "client_secret": "CONNECTED_APP_CONSUMER_SECRET",
    "refresh_token": "REFRESH_TOKEN",
    "instance_url":  "https://your-org.my.salesforce.com",
}
```

## Usage

### Pull records (generic SOQL — any standard or custom SObject)

```python
from aisquare.pipe import PullParams
from aisquare_pipe_salesforce import SalesforceSource

source = SalesforceSource()
params = PullParams(params={
    "object_type": "Account",                 # required
    "fields": ["Id", "Name", "Industry"],     # optional, default: Id/Name/CreatedDate/LastModifiedDate
    "where": "Industry = 'Technology'",       # optional
    "order_by": "CreatedDate DESC",           # optional
    "limit": 100,                             # optional
    "modified_after": "2024-01-01T00:00:00Z", # optional — incremental sync
})

for envelope in source.pull(config, params):
    print(envelope.metadata["salesforce_id"], envelope.data)
```

Need a more complex query? Pass `soql` directly and the connector will use it verbatim:

```python
params = PullParams(params={
    "object_type": "Account",  # still required for metadata tagging
    "soql": "SELECT Id, Name, (SELECT Id FROM Contacts) FROM Account LIMIT 10",
})
```

### Push records (insert / update / upsert)

```python
from aisquare.pipe import DataEnvelope, PushParams
from aisquare_pipe_salesforce import SalesforceSink

sink = SalesforceSink()

# Insert (no salesforce_id in metadata)
envelope = DataEnvelope(
    content_type="application/json",
    data={"Name": "Acme Corp", "Industry": "Technology"},
    source_id="my-app",
    metadata={"object_type": "Account"},
)
result = sink.push(envelope, config)
print(f"Created Account: {result.ref}")

# Update (salesforce_id in metadata → inferred as update)
envelope = DataEnvelope(
    content_type="application/json",
    data={"Industry": "Healthcare"},
    source_id="my-app",
    metadata={"object_type": "Account", "salesforce_id": "0011x0000XXXXXX"},
)
sink.push(envelope, config)

# Upsert by external id
envelope = DataEnvelope(
    content_type="application/json",
    data={"Name": "Acme Corp", "Industry": "Tech"},
    source_id="my-app",
    metadata={"object_type": "Account", "external_id_field": "External_Id__c"},
)
sink.push(envelope, config, PushParams(params={"external_id_value": "ext-123"}))
```

### Pipeline (Salesforce → local)

```python
from aisquare.pipe import Pipeline, PullParams

result = Pipeline(source=SalesforceSource(), sink=LocalSink()).run(
    {
        "salesforce-source": config,
        "local-sink": {"output_dir": "./accounts"},
    },
    pull_params=PullParams(params={"object_type": "Account", "limit": 50}),
)
```

## Features

- **Generic SObject support** — Account, Contact, Lead, Opportunity, custom objects (`Foo__c`), all via one connector
- **Dual auth** — username/password+token (dev) and OAuth2 refresh-token (production)
- **Incremental sync** — `modified_after` param appends `LastModifiedDate > <ts>` to the SOQL
- **SOQL escape hatch** — pass `soql` verbatim for joins, sub-queries, aggregates
- **Insert / update / upsert dispatch** — inferred from metadata, or set explicitly via `params["operation"]`
- **Rate-limit-aware** — exponential backoff on `REQUEST_LIMIT_EXCEEDED` / HTTP 503

## Notes

- For very large pulls, set a reasonable `limit` and use `modified_after` for incremental syncs to avoid hitting per-call governor limits.
- OAuth2 access tokens are exchanged at client creation; for long-running pulls that span the access-token TTL, re-create the source.
- Salesforce `attributes` keys (the SDK's metadata noise) are stripped from each record before yielding.
