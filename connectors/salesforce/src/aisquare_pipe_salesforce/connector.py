"""Salesforce source + sink connectors for aisquare.pipe."""

from __future__ import annotations

import mimetypes
from collections.abc import Iterator
from typing import Any

from aisquare.pipe.core.connector import AuthType, SinkConnector, SourceConnector
from aisquare.pipe.core.envelope import (
    DataEnvelope,
    MetaField,
    PullParams,
    PushParams,
    PushResult,
)

from aisquare_pipe_salesforce.client import SalesforceClient, SalesforceError


class SalesforceSource(SourceConnector):
    """Pull record attachments (ContentVersion files) from Salesforce."""

    name = "salesforce-source"
    version = "0.1.0"
    output_types = ["*/*"]
    auth_type = AuthType.OAUTH2
    description = "Pull ContentVersion attachments from Salesforce records"
    docs_url = "https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/"
    metadata_spec = {
        "content_version_id": MetaField(
            type=str, required=True, description="Salesforce ContentVersion id"
        ),
        "filename": MetaField(type=str, required=False, description="Original filename"),
    }

    def pull(
        self, config: dict, params: PullParams | None = None
    ) -> Iterator[DataEnvelope]:
        """Yield one DataEnvelope per requested ContentVersion.

        Supported PullParams keys:
            content_version_ids (list[str]): ContentVersion ids to download.
            content_version_id (str): single-id convenience form.
            filename (str): used for MIME sniffing + the filename metadata.
        """
        client = SalesforceClient(config)
        ids: list[str] = []
        if params:
            ids = list(params.get("content_version_ids") or [])
            single = params.get("content_version_id")
            if single:
                ids.append(single)
        filename = params.get("filename", "") if params else ""
        mime_type, _ = mimetypes.guess_type(filename or "")

        for content_version_id in ids:
            data = client.download_content_version(content_version_id)
            yield DataEnvelope(
                content_type=mime_type or "application/octet-stream",
                data=data,
                source_id=self.name,
                metadata={
                    "content_version_id": content_version_id,
                    "filename": filename,
                },
            )

    def validate_config(self, config: dict) -> bool:
        return SalesforceClient(config).validate()


class SalesforceSink(SinkConnector):
    """Push JSON field updates onto a Salesforce sObject record.

    The envelope ``data`` is the field map (e.g. host-state custom fields);
    ``params`` carry the target: ``sobject`` + ``record_id``.
    """

    name = "salesforce-sink"
    version = "0.1.0"
    input_types = ["application/json"]
    auth_type = AuthType.OAUTH2
    description = "PATCH field updates onto Salesforce sObject records"
    docs_url = "https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/"

    def push(
        self,
        envelope: DataEnvelope,
        config: dict,
        params: PushParams | None = None,
    ) -> PushResult:
        sobject = params.get("sobject", "Contract") if params else "Contract"
        record_id = params.get("record_id", "") if params else ""
        if not record_id:
            return PushResult(success=False, error="params.record_id is required")
        fields = envelope.data if isinstance(envelope.data, dict) else None
        if not isinstance(fields, dict) or not fields:
            return PushResult(success=False, error="envelope.data must be a non-empty dict")
        try:
            SalesforceClient(config).update_sobject(sobject, record_id, fields)
        except SalesforceError as exc:
            return PushResult(success=False, error=str(exc))
        return PushResult(
            success=True, ref=record_id, metadata={"sobject": sobject, "fields": list(fields)}
        )

    def validate_config(self, config: dict) -> bool:
        return SalesforceClient(config).validate()
