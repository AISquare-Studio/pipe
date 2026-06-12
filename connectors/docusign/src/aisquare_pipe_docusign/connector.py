"""DocuSign source + sink connectors for aisquare.pipe."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from typing import Any

from aisquare.pipe.core.connector import AuthType, SinkConnector, SourceConnector
from aisquare.pipe.core.envelope import (
    DataEnvelope,
    MetaField,
    PullParams,
    PushParams,
    PushResult,
    Resource,
)

from aisquare_pipe_docusign.client import DocuSignClient, DocuSignError


class DocuSignSink(SinkConnector):
    """Send a PDF out for signature as a DocuSign envelope.

    The envelope ``data`` is the PDF bytes; ``params`` carry recipients and
    email copy. Returns ``PushResult.ref`` = the DocuSign envelope id.
    """

    name = "docusign-sink"
    version = "0.1.0"
    input_types = ["application/pdf"]
    auth_type = AuthType.OAUTH2
    description = "Create + send DocuSign envelopes for signature"
    docs_url = "https://developers.docusign.com/docs/esign-rest-api/"

    def push(
        self,
        envelope: DataEnvelope,
        config: dict,
        params: PushParams | None = None,
    ) -> PushResult:
        if not isinstance(envelope.data, bytes) or not envelope.data:
            return PushResult(success=False, error="envelope.data must be PDF bytes")
        recipients = list(params.get("recipients") or []) if params else []
        if not recipients:
            return PushResult(success=False, error="params.recipients is required")

        document_name = (
            (params.get("document_name") if params else None)
            or envelope.metadata.get("filename")
            or "document.pdf"
        )
        payload: dict[str, Any] = {
            "emailSubject": params.get("email_subject", "Please sign") if params else "Please sign",
            "emailBlurb": params.get("email_message", "") if params else "",
            "status": params.get("status", "sent") if params else "sent",
            "documents": [
                {
                    "documentBase64": base64.b64encode(envelope.data).decode(),
                    "name": document_name,
                    "fileExtension": "pdf",
                    "documentId": "1",
                }
            ],
            "recipients": {
                "signers": [
                    {
                        "name": r.get("name", ""),
                        "email": r.get("email", ""),
                        "recipientId": str(i + 1),
                        "routingOrder": str(r.get("routingOrder", r.get("routing_order", 1))),
                    }
                    for i, r in enumerate(recipients)
                ]
            },
        }
        try:
            envelope_id = DocuSignClient(config).create_envelope(payload)
        except DocuSignError as exc:
            return PushResult(success=False, error=str(exc))
        return PushResult(success=True, ref=envelope_id)

    def validate_config(self, config: dict) -> bool:
        return DocuSignClient(config).validate()


class DocuSignSource(SourceConnector):
    """Pull signed combined documents; browse in-flight envelopes."""

    name = "docusign-source"
    version = "0.1.0"
    output_types = ["application/pdf"]
    auth_type = AuthType.OAUTH2
    description = "Pull signed (combined) PDFs from DocuSign envelopes"
    docs_url = "https://developers.docusign.com/docs/esign-rest-api/"
    metadata_spec = {
        "envelope_id": MetaField(type=str, required=True, description="DocuSign envelope id"),
        "status": MetaField(type=str, required=False, description="Envelope status"),
    }

    def pull(
        self, config: dict, params: PullParams | None = None
    ) -> Iterator[DataEnvelope]:
        """Yield the signed combined PDF for each requested envelope.

        Supported PullParams keys:
            envelope_ids (list[str]) / envelope_id (str): envelopes to fetch.
        """
        client = DocuSignClient(config)
        ids: list[str] = []
        if params:
            ids = list(params.get("envelope_ids") or [])
            single = params.get("envelope_id")
            if single:
                ids.append(single)
        for envelope_id in ids:
            data = client.download_combined_document(envelope_id)
            yield DataEnvelope(
                content_type="application/pdf",
                data=data,
                source_id=self.name,
                metadata={"envelope_id": envelope_id},
            )

    def list_resources(self, config: dict) -> list[Resource]:
        """Browse envelopes changed in the last 30 days (sync backstop)."""
        from datetime import datetime, timedelta, timezone

        from_date = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        return [
            Resource(
                id=e.get("envelopeId", ""),
                name=e.get("emailSubject", "") or e.get("envelopeId", ""),
                resource_type="envelope",
                metadata={"status": e.get("status", "")},
            )
            for e in DocuSignClient(config).list_envelopes(from_date)
        ]

    def validate_config(self, config: dict) -> bool:
        return DocuSignClient(config).validate()
