"""DocuSign connector tests — client request shapes (mocked session),
sink envelope payloads, source pulls, Connect HMAC vectors."""

from __future__ import annotations

import base64
import hashlib
import hmac
from unittest.mock import MagicMock

import pytest

from aisquare.pipe.core.envelope import DataEnvelope, PullParams, PushParams
from aisquare_pipe_docusign.client import DocuSignAuthError, DocuSignClient
from aisquare_pipe_docusign.connector import DocuSignSink, DocuSignSource
from aisquare_pipe_docusign.webhook import verify_connect_hmac


@pytest.fixture
def config() -> dict:
    return {
        "integration_key": "ik",
        "client_secret": "cs",
        "access_token": "tok",
        "account_id": "acct",
        "base_uri": "https://demo.docusign.net",
    }


def _response(status_code=200, json_data=None, content=b""):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_data or {}
    response.content = content
    response.text = ""
    return response


class TestClient:
    def test_create_envelope_posts_to_account_path(self, config):
        session = MagicMock()
        session.post.return_value = _response(json_data={"envelopeId": "env-1"})
        envelope_id = DocuSignClient(config, session=session).create_envelope({"a": 1})
        assert envelope_id == "env-1"
        assert "/restapi/v2.1/accounts/acct/envelopes" in session.post.call_args[0][0]

    def test_download_combined_document(self, config):
        session = MagicMock()
        session.get.return_value = _response(content=b"%PDF signed")
        data = DocuSignClient(config, session=session).download_combined_document("env-1")
        assert data == b"%PDF signed"
        assert "/envelopes/env-1/documents/combined" in session.get.call_args[0][0]

    def test_auth_error_mapping(self, config):
        session = MagicMock()
        session.get.return_value = _response(status_code=401)
        with pytest.raises(DocuSignAuthError):
            DocuSignClient(config, session=session).envelope_status("env-1")


class TestSinkAndSource:
    def test_sink_builds_envelope_payload(self, config, monkeypatch):
        captured = {}

        def fake_create(self, payload):
            captured.update(payload)
            return "env-9"

        monkeypatch.setattr(DocuSignClient, "create_envelope", fake_create)
        result = DocuSignSink().push(
            DataEnvelope(
                content_type="application/pdf",
                data=b"%PDF",
                source_id="host",
                metadata={"filename": "msa.pdf"},
            ),
            config,
            PushParams(
                {
                    "recipients": [{"name": "Priya", "email": "p@x.com", "routingOrder": 1}],
                    "email_subject": "Please sign the MSA",
                }
            ),
        )
        assert result.success and result.ref == "env-9"
        assert captured["documents"][0]["name"] == "msa.pdf"
        assert captured["documents"][0]["documentBase64"] == base64.b64encode(b"%PDF").decode()
        assert captured["recipients"]["signers"][0]["email"] == "p@x.com"
        assert captured["emailSubject"] == "Please sign the MSA"

    def test_sink_requires_recipients_and_bytes(self, config):
        sink = DocuSignSink()
        no_recipients = sink.push(
            DataEnvelope(content_type="application/pdf", data=b"%PDF", source_id="x"), config
        )
        assert not no_recipients.success
        no_bytes = sink.push(
            DataEnvelope(content_type="application/pdf", data="", source_id="x"),
            config,
            PushParams({"recipients": [{"name": "P", "email": "p@x.com"}]}),
        )
        assert not no_bytes.success

    def test_source_pulls_signed_pdf(self, config, monkeypatch):
        monkeypatch.setattr(
            DocuSignClient,
            "download_combined_document",
            lambda self, envelope_id: b"%PDF " + envelope_id.encode(),
        )
        envelopes = list(
            DocuSignSource().pull(config, PullParams({"envelope_id": "env-1"}))
        )
        assert len(envelopes) == 1
        assert envelopes[0].content_type == "application/pdf"
        assert envelopes[0].metadata["envelope_id"] == "env-1"

    def test_list_resources_browses_envelopes(self, config, monkeypatch):
        monkeypatch.setattr(
            DocuSignClient,
            "list_envelopes",
            lambda self, from_date: [
                {"envelopeId": "env-1", "emailSubject": "MSA", "status": "sent"}
            ],
        )
        resources = DocuSignSource().list_resources(config)
        assert resources[0].id == "env-1"
        assert resources[0].metadata["status"] == "sent"

    def test_validate_config(self, config):
        assert DocuSignSink().validate_config(config)
        assert not DocuSignSink().validate_config({"access_token": "tok"})


class TestWebhook:
    def test_connect_hmac_vectors(self):
        body, key = b'{"envelopeId": "env-1"}', "connect-key"
        good = base64.b64encode(hmac.new(key.encode(), body, hashlib.sha256).digest()).decode()
        assert verify_connect_hmac(body, good, key)
        assert not verify_connect_hmac(body, good, "wrong")
        assert not verify_connect_hmac(body, "tampered", key)
