"""Salesforce source and sink connectors for aisquare.pipe."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

from aisquare.pipe.core.connector import AuthType, SinkConnector, SourceConnector
from aisquare.pipe.core.envelope import (
    DataEnvelope,
    MetaField,
    PullParams,
    PushParams,
    PushResult,
    RateLimit,
    Resource,
)

from aisquare_pipe_salesforce.auth import has_valid_auth_keys
from aisquare_pipe_salesforce.client import SalesforceClient
from aisquare_pipe_salesforce.constants import DEFAULT_BATCH_SIZE, DEFAULT_FIELDS

logger = logging.getLogger("aisquare.pipe.salesforce")


def _build_soql(
    object_type: str,
    fields: list[str],
    where: str | None,
    order_by: str | None,
    limit: int | None,
    modified_after: str | None,
) -> str:
    """Compose a SOQL query from PullParams."""
    field_list = ", ".join(fields)
    soql = f"SELECT {field_list} FROM {object_type}"

    clauses: list[str] = []
    if where:
        clauses.append(f"({where})")
    if modified_after:
        clauses.append(f"LastModifiedDate > {modified_after}")
    if clauses:
        soql += " WHERE " + " AND ".join(clauses)

    if order_by:
        soql += f" ORDER BY {order_by}"
    if limit is not None:
        soql += f" LIMIT {limit}"
    return soql


def _strip_attributes(record: dict[str, Any]) -> dict[str, Any]:
    """Remove the Salesforce SDK's `attributes` noise key from a record."""
    return {k: v for k, v in record.items() if k != "attributes"}


def _coerce_to_dict(data: Any) -> dict[str, Any] | None:
    """Resolve envelope.data → a dict suitable for Salesforce record APIs."""
    if isinstance(data, dict):
        return data
    if isinstance(data, str):
        return json.loads(data)
    if isinstance(data, bytes):
        return json.loads(data.decode("utf-8"))
    return None


class SalesforceSource(SourceConnector):
    """Pull records from Salesforce via SOQL — any standard or custom SObject."""

    name = "salesforce-source"
    version = "0.1.0"
    output_types = ["application/json"]
    auth_type = AuthType.OAUTH2
    description = "Pull records from Salesforce SObjects via SOQL"
    docs_url = "https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/"
    metadata_spec = {
        "salesforce_id": MetaField(
            type=str, required=True, description="SObject record Id"
        ),
        "object_type": MetaField(
            type=str, required=True, description="SObject API name (Account, Contact, Foo__c, ...)"
        ),
        "created_date": MetaField(
            type=str, required=False, description="ISO timestamp of record creation"
        ),
        "last_modified_date": MetaField(
            type=str, required=False, description="ISO timestamp of last modification"
        ),
    }

    def pull(
        self, config: dict, params: PullParams | None = None
    ) -> Iterator[DataEnvelope]:
        """Yield a DataEnvelope per Salesforce record matching the SOQL.

        Supported PullParams keys:
            object_type (str, required): SObject API name (e.g. "Account", "Foo__c")
            fields (list[str]): columns to select (default: Id, Name, CreatedDate, LastModifiedDate)
            where (str): WHERE clause body (without the "WHERE" keyword)
            order_by (str): ORDER BY clause body
            limit (int): record cap
            modified_after (str): ISO datetime — appends LastModifiedDate > <ts>
            batch_size (int): records per SOQL page (default 200)
            soql (str): escape hatch — used verbatim, ignoring all the above except object_type
        """
        if params is None:
            params = PullParams()

        object_type = params.get("object_type")
        if not object_type:
            raise ValueError("salesforce-source.pull requires params['object_type']")

        client = SalesforceClient(config)

        soql = params.get("soql")
        if not soql:
            soql = _build_soql(
                object_type=object_type,
                fields=params.get("fields", DEFAULT_FIELDS),
                where=params.get("where"),
                order_by=params.get("order_by"),
                limit=params.get("limit"),
                modified_after=params.get("modified_after"),
            )

        batch_size = params.get("batch_size", DEFAULT_BATCH_SIZE)
        logger.debug("Executing SOQL: %s", soql)

        for record in client.query_iter(soql, batch_size=batch_size):
            clean = _strip_attributes(record)
            metadata = {
                "salesforce_id": clean.get("Id"),
                "object_type": object_type,
            }
            if "CreatedDate" in clean:
                metadata["created_date"] = clean["CreatedDate"]
            if "LastModifiedDate" in clean:
                metadata["last_modified_date"] = clean["LastModifiedDate"]

            yield DataEnvelope(
                content_type="application/json",
                data=clean,
                source_id=self.name,
                metadata=metadata,
            )

    def validate_config(self, config: dict) -> bool:
        if not has_valid_auth_keys(config):
            return False
        try:
            return SalesforceClient(config).validate()
        except Exception:
            return False

    def list_resources(self, config: dict) -> list[Resource]:
        """Browse queryable SObjects in the connected org."""
        client = SalesforceClient(config)
        resources: list[Resource] = []
        for sobj in client.describe_sobjects():
            if not sobj.get("queryable"):
                continue
            resources.append(
                Resource(
                    id=sobj["name"],
                    name=sobj.get("label", sobj["name"]),
                    resource_type="sobject",
                    metadata={
                        "custom": sobj.get("custom", False),
                        "createable": sobj.get("createable", False),
                        "updateable": sobj.get("updateable", False),
                    },
                )
            )
        return resources

    def rate_limit(self) -> RateLimit | None:
        return RateLimit(requests_per_second=10, concurrent=5)


class SalesforceSink(SinkConnector):
    """Push records to Salesforce — insert, update, or upsert any SObject."""

    name = "salesforce-sink"
    version = "0.1.0"
    input_types = ["application/json"]
    auth_type = AuthType.OAUTH2
    description = "Push records (insert/update/upsert) to Salesforce SObjects"
    docs_url = "https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/"
    metadata_spec = {
        "object_type": MetaField(
            type=str, required=True, description="Target SObject API name"
        ),
        "salesforce_id": MetaField(
            type=str,
            required=False,
            description="Existing record Id (presence triggers update)",
        ),
        "external_id_field": MetaField(
            type=str,
            required=False,
            description="External-id field name (presence triggers upsert)",
        ),
    }

    def push(
        self,
        envelope: DataEnvelope,
        config: dict,
        params: PushParams | None = None,
    ) -> PushResult:
        """Push an envelope into Salesforce.

        Operation dispatch:
            - params["operation"] in {"insert", "update", "upsert"} wins if set.
            - Else: external_id_field → upsert; salesforce_id → update; else → insert.

        Supported PushParams keys:
            operation (str): explicit override — "insert" | "update" | "upsert"
            object_type (str): override metadata
            external_id_value (str): required for upsert (the value of external_id_field)
        """
        try:
            data = _coerce_to_dict(envelope.data)
            if data is None:
                return PushResult(
                    success=False,
                    error=f"Unsupported data type: {type(envelope.data).__name__}",
                )

            meta = envelope.metadata
            params = params or PushParams()

            object_type = params.get("object_type") or meta.get("object_type")
            if not object_type:
                return PushResult(
                    success=False, error="Missing object_type in metadata or params"
                )

            external_id_field = meta.get("external_id_field")
            salesforce_id = meta.get("salesforce_id")
            operation = params.get("operation") or (
                "upsert"
                if external_id_field
                else "update"
                if salesforce_id
                else "insert"
            )

            client = SalesforceClient(config)

            if operation == "insert":
                result = client.create(object_type, data)
                ref = result.get("id") if isinstance(result, dict) else None
                return PushResult(
                    success=True,
                    ref=ref,
                    metadata={"operation": "insert", "object_type": object_type},
                )

            if operation == "update":
                if not salesforce_id:
                    return PushResult(
                        success=False,
                        error="update operation requires metadata['salesforce_id']",
                    )
                status = client.update(object_type, salesforce_id, data)
                return PushResult(
                    success=True,
                    ref=salesforce_id,
                    metadata={
                        "operation": "update",
                        "object_type": object_type,
                        "status": status,
                    },
                )

            if operation == "upsert":
                if not external_id_field:
                    return PushResult(
                        success=False,
                        error="upsert operation requires metadata['external_id_field']",
                    )
                external_id_value = params.get("external_id_value")
                if not external_id_value:
                    return PushResult(
                        success=False,
                        error="upsert operation requires params['external_id_value']",
                    )
                status = client.upsert(
                    object_type, external_id_field, external_id_value, data
                )
                return PushResult(
                    success=True,
                    ref=external_id_value,
                    metadata={
                        "operation": "upsert",
                        "object_type": object_type,
                        "status": status,
                    },
                )

            return PushResult(success=False, error=f"Unknown operation: {operation}")

        except Exception as e:
            logger.error("Salesforce push failed: %s", e)
            return PushResult(success=False, error=str(e))

    def validate_config(self, config: dict) -> bool:
        if not has_valid_auth_keys(config):
            return False
        try:
            return SalesforceClient(config).validate()
        except Exception:
            return False
