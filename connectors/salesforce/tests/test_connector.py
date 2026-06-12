"""Salesforce connector tests — client request shapes (mocked session),
source/sink behavior, OAuth helpers returning tokens, webhook HMAC vectors."""

from __future__ import annotations

import hashlib
import hmac
from unittest.mock import MagicMock

import pytest

from aisquare.pipe.core.envelope import DataEnvelope, PullParams, PushParams
from aisquare_pipe_salesforce.client import (
    SalesforceAuthError,
    SalesforceClient,
    SalesforceRateLimited,
    refresh_access_token,
)
from aisquare_pipe_salesforce.connector import SalesforceSink, SalesforceSource
from aisquare_pipe_salesforce.webhook import timestamp_in_window, verify_webhook_signature


@pytest.fixture
def config() -> dict:
    return {
        "client_id": "cid",
        "client_secret": "secret",
        "access_token": "tok",
        "refresh_token": "ref",
        "instance_url": "https://org.my.salesforce.com",
    }


def _response(status_code=200, json_data=None, content=b""):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_data or {}
    response.content = content
    response.text = ""
    return response


class TestClient:
    def test_update_sobject_patches_configured_path(self, config):
        session = MagicMock()
        session.patch.return_value = _response()
        SalesforceClient(config, session=session).update_sobject(
            "Contract", "0011J", {"MS_V2_State__c": "signed"}
        )
        args, kwargs = session.patch.call_args
        assert "/services/data/v60.0/sobjects/Contract/0011J" in args[0]
        assert kwargs["json"] == {"MS_V2_State__c": "signed"}
        assert kwargs["headers"]["Authorization"] == "Bearer tok"

    def test_download_content_version(self, config):
        session = MagicMock()
        session.get.return_value = _response(content=b"DOCX bytes")
        data = SalesforceClient(config, session=session).download_content_version("068A")
        assert data == b"DOCX bytes"
        assert "/sobjects/ContentVersion/068A/VersionData" in session.get.call_args[0][0]

    def test_error_mapping(self, config):
        session = MagicMock()
        session.patch.return_value = _response(status_code=401)
        with pytest.raises(SalesforceAuthError):
            SalesforceClient(config, session=session).update_sobject("Contract", "x", {})
        session.patch.return_value = _response(status_code=429)
        with pytest.raises(SalesforceRateLimited):
            SalesforceClient(config, session=session).update_sobject("Contract", "x", {})

    def test_refresh_returns_tokens_for_host_to_persist(self, config):
        session = MagicMock()
        session.post.return_value = _response(json_data={"access_token": "new"})
        tokens = refresh_access_token(config, session=session)
        assert tokens == {"access_token": "new"}
        assert session.post.call_args[1]["data"]["grant_type"] == "refresh_token"


class TestSourceAndSink:
    def test_source_yields_envelope_per_content_version(self, config, monkeypatch):
        monkeypatch.setattr(
            SalesforceClient, "download_content_version", lambda self, cid: b"bytes-" + cid.encode()
        )
        envelopes = list(
            SalesforceSource().pull(
                config,
                PullParams({"content_version_id": "068A", "filename": "msa.docx"}),
            )
        )
        assert len(envelopes) == 1
        assert envelopes[0].data == b"bytes-068A"
        assert envelopes[0].metadata["content_version_id"] == "068A"
        assert "wordprocessingml" in envelopes[0].content_type

    def test_sink_pushes_field_updates(self, config, monkeypatch):
        calls = {}

        def fake_update(self, sobject, record_id, fields):
            calls.update(sobject=sobject, record_id=record_id, fields=fields)

        monkeypatch.setattr(SalesforceClient, "update_sobject", fake_update)
        result = SalesforceSink().push(
            DataEnvelope(
                content_type="application/json",
                data={"MS_V2_State__c": "signed"},
                source_id="host",
            ),
            config,
            PushParams({"sobject": "Contract", "record_id": "0011J"}),
        )
        assert result.success and result.ref == "0011J"
        assert calls["fields"] == {"MS_V2_State__c": "signed"}

    def test_sink_requires_record_id_and_fields(self, config):
        sink = SalesforceSink()
        no_target = sink.push(
            DataEnvelope(content_type="application/json", data={"a": 1}, source_id="x"), config
        )
        assert not no_target.success
        no_fields = sink.push(
            DataEnvelope(content_type="application/json", data={}, source_id="x"),
            config,
            PushParams({"record_id": "0011J"}),
        )
        assert not no_fields.success

    def test_validate_config(self, config):
        assert SalesforceSource().validate_config(config)
        assert not SalesforceSource().validate_config({"access_token": "tok"})


class TestWebhook:
    def test_signature_vectors(self):
        body, secret = b'{"event_id": "e1"}', "shh"
        good = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert verify_webhook_signature(body, good, secret)
        assert not verify_webhook_signature(body, "sha256=deadbeef", secret)
        assert not verify_webhook_signature(body, good, "wrong")
        assert not verify_webhook_signature(body, "", secret)

    def test_timestamp_window(self):
        assert timestamp_in_window("1000", now=1100.0)
        assert not timestamp_in_window("1000", now=1400.0)
        assert not timestamp_in_window("garbage", now=1000.0)
