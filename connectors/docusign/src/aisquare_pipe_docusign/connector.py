"""DocuSign source and sink connectors for aisquare.pipe."""

from __future__ import annotations

import base64
import logging
from collections.abc import Iterator
from typing import Any

from docusign_esign import (
    Document,
    EnvelopeDefinition,
    Recipients,
    SignHere,
    Signer,
    Tabs,
)

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

from aisquare_pipe_docusign.auth import has_valid_auth_keys
from aisquare_pipe_docusign.client import DocusignClient
from aisquare_pipe_docusign.constants import DEFAULT_SUBJECT

logger = logging.getLogger("aisquare.pipe.docusign")


def _envelope_metadata(env: Any) -> dict[str, Any]:
    """Extract a flat metadata dict from a docusign-esign Envelope object."""
    return {
        "envelope_id": getattr(env, "envelope_id", None),
        "status": getattr(env, "status", None),
        "subject": getattr(env, "email_subject", None),
        "sender_email": getattr(getattr(env, "sender", None), "email", None),
        "created_date": getattr(env, "created_date_time", None),
        "completed_date": getattr(env, "completed_date_time", None),
    }


def _coerce_to_bytes(data: Any, stream: Any) -> bytes | None:
    """Resolve envelope data/stream to raw bytes for upload."""
    if stream is not None:
        return stream.read()
    if isinstance(data, bytes):
        return data
    if isinstance(data, str):
        return data.encode("utf-8")
    return None


class DocusignSource(SourceConnector):
    """Pull signed documents and envelope metadata from DocuSign."""

    name = "docusign-source"
    version = "0.1.0"
    output_types = ["application/pdf", "application/json"]
    auth_type = AuthType.OAUTH2
    description = "Pull signed documents and envelope metadata from DocuSign"
    docs_url = "https://developers.docusign.com/docs/esign-rest-api/"
    metadata_spec = {
        "envelope_id": MetaField(
            type=str, required=True, description="DocuSign envelope GUID"
        ),
        "document_id": MetaField(
            type=str,
            required=False,
            description="Document id within envelope (documents mode only)",
        ),
        "filename": MetaField(
            type=str, required=False, description="Document filename (documents mode)"
        ),
        "status": MetaField(
            type=str,
            required=True,
            description="Envelope status: sent, delivered, completed, ...",
        ),
        "subject": MetaField(type=str, required=False, description="Email subject"),
        "sender_email": MetaField(
            type=str, required=False, description="Sender's email"
        ),
        "created_date": MetaField(
            type=str, required=False, description="ISO timestamp"
        ),
        "completed_date": MetaField(
            type=str, required=False, description="ISO timestamp"
        ),
    }

    def pull(
        self, config: dict, params: PullParams | None = None
    ) -> Iterator[DataEnvelope]:
        """Yield DataEnvelopes for DocuSign envelopes or their documents.

        Supported PullParams keys:
            mode (str): "documents" (default) or "envelopes"
            status (str): envelope status filter ("completed", "sent", ...)
            from_date (str): ISO date — envelopes modified after
            to_date (str): ISO date — envelopes modified before
            folder_id (str): restrict to a DocuSign folder
            envelope_ids (list[str]): specific envelopes; skips listing
            include_combined (bool): in documents mode, also yield the combined PDF
            limit (int): cap total envelopes
            account_id (str): override auto-discovered account
        """
        if params is None:
            params = PullParams()

        mode = params.get("mode", "documents")
        if mode not in ("documents", "envelopes"):
            raise ValueError(f"Unknown mode: {mode!r} (expected 'documents' or 'envelopes')")

        client = DocusignClient(config)

        envelopes = client.list_envelopes(
            status=params.get("status"),
            from_date=params.get("from_date"),
            to_date=params.get("to_date"),
            folder_id=params.get("folder_id"),
            envelope_ids=params.get("envelope_ids"),
            limit=params.get("limit"),
        )

        include_combined = params.get("include_combined", False)

        for env in envelopes:
            env_meta = _envelope_metadata(env)
            envelope_id = env_meta["envelope_id"]

            if mode == "envelopes":
                yield DataEnvelope(
                    content_type="application/json",
                    data=env.to_dict() if hasattr(env, "to_dict") else env_meta,
                    source_id=self.name,
                    metadata=env_meta,
                )
                continue

            documents = client.list_documents(envelope_id)
            for doc in documents:
                doc_id = getattr(doc, "document_id", None)
                if doc_id == "combined" and not include_combined:
                    continue
                pdf_bytes = client.get_document_bytes(envelope_id, doc_id)
                meta = {
                    **env_meta,
                    "document_id": doc_id,
                    "filename": getattr(doc, "name", None),
                }
                yield DataEnvelope(
                    content_type="application/pdf",
                    data=pdf_bytes,
                    source_id=self.name,
                    metadata=meta,
                )

    def validate_config(self, config: dict) -> bool:
        if not has_valid_auth_keys(config):
            return False
        try:
            return DocusignClient(config).validate()
        except Exception:
            return False

    def list_resources(self, config: dict) -> list[Resource]:
        """Browse the user's DocuSign folders."""
        client = DocusignClient(config)
        resources: list[Resource] = []
        for folder in client.list_folders():
            resources.append(
                Resource(
                    id=getattr(folder, "folder_id", "") or "",
                    name=getattr(folder, "name", "") or "",
                    resource_type="folder",
                    metadata={
                        "type": getattr(folder, "type", None),
                        "owner_email": getattr(folder, "owner_email", None),
                    },
                )
            )
        return resources

    def rate_limit(self) -> RateLimit | None:
        return RateLimit(requests_per_second=3, concurrent=2)


class DocusignSink(SinkConnector):
    """Send documents to DocuSign for signature."""

    name = "docusign-sink"
    version = "0.1.0"
    input_types = ["application/pdf"]
    auth_type = AuthType.OAUTH2
    description = "Send documents to DocuSign for signature"
    docs_url = "https://developers.docusign.com/docs/esign-rest-api/"
    metadata_spec = {
        "filename": MetaField(
            type=str, required=True, description="Document filename shown to signers"
        ),
        "signers": MetaField(
            type=list,
            required=True,
            description="List of {name, email, recipient_id?, routing_order?} dicts",
        ),
        "subject": MetaField(
            type=str, required=False, description="Email subject for the envelope"
        ),
        "email_blurb": MetaField(
            type=str, required=False, description="Email body for the envelope"
        ),
    }

    def push(
        self,
        envelope: DataEnvelope,
        config: dict,
        params: PushParams | None = None,
    ) -> PushResult:
        """Create a DocuSign envelope from the input PDF and request signatures.

        Supported PushParams keys:
            status (str): "sent" (default) → send immediately; "created" → save as draft
        """
        try:
            pdf_bytes = _coerce_to_bytes(envelope.data, envelope.stream)
            if pdf_bytes is None:
                return PushResult(
                    success=False,
                    error=f"Unsupported data type: {type(envelope.data).__name__}",
                )

            meta = envelope.metadata
            filename = meta.get("filename")
            signers_meta = meta.get("signers")
            if not filename:
                return PushResult(
                    success=False, error="Missing required metadata['filename']"
                )
            if not signers_meta:
                return PushResult(
                    success=False, error="Missing required metadata['signers']"
                )

            params = params or PushParams()
            status = params.get("status", "sent")
            subject = meta.get("subject", DEFAULT_SUBJECT)
            email_blurb = meta.get("email_blurb", "")

            document = Document(
                document_base64=base64.b64encode(pdf_bytes).decode("ascii"),
                name=filename,
                file_extension="pdf",
                document_id="1",
            )

            signers = []
            for i, s in enumerate(signers_meta, start=1):
                signer = Signer(
                    email=s["email"],
                    name=s["name"],
                    recipient_id=str(s.get("recipient_id", i)),
                    routing_order=str(s.get("routing_order", i)),
                    tabs=Tabs(
                        sign_here_tabs=[
                            SignHere(
                                anchor_string=s.get("anchor_string", "/sign_here/"),
                                anchor_units="pixels",
                                anchor_y_offset="0",
                                anchor_x_offset="0",
                            )
                        ]
                    ),
                )
                signers.append(signer)

            envelope_definition = EnvelopeDefinition(
                email_subject=subject,
                email_blurb=email_blurb,
                documents=[document],
                recipients=Recipients(signers=signers),
                status=status,
            )

            client = DocusignClient(config)
            result = client.create_envelope(envelope_definition)
            envelope_id = getattr(result, "envelope_id", None)

            return PushResult(
                success=True,
                ref=envelope_id,
                metadata={
                    "envelope_id": envelope_id,
                    "status": getattr(result, "status", status),
                },
            )

        except Exception as e:
            logger.error("DocuSign push failed: %s", e)
            return PushResult(success=False, error=str(e))

    def validate_config(self, config: dict) -> bool:
        if not has_valid_auth_keys(config):
            return False
        try:
            return DocusignClient(config).validate()
        except Exception:
            return False
