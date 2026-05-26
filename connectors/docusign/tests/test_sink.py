"""Tests for DocusignSink."""

from __future__ import annotations

import base64
import io
from unittest.mock import MagicMock

from aisquare.pipe.core.envelope import DataEnvelope, PushParams, PushResult

from aisquare_pipe_docusign.connector import DocusignSink


def _envelope(data, metadata=None, stream=None) -> DataEnvelope:
    return DataEnvelope(
        content_type="application/pdf",
        data=data,
        source_id="test",
        metadata=metadata or {},
        stream=stream,
    )


_DEFAULT_META = {
    "filename": "contract.pdf",
    "signers": [
        {"name": "Alice", "email": "alice@example.com"},
        {"name": "Bob",   "email": "bob@example.com"},
    ],
    "subject": "Please sign the contract",
    "email_blurb": "Sign at your convenience.",
}


class TestPushHappyPath:
    def test_creates_envelope_and_returns_id(self, mock_client, sample_jwt_config):
        mock_client.create_envelope.return_value = MagicMock(
            envelope_id="env-new", status="sent"
        )
        result = DocusignSink().push(
            _envelope(b"%PDF-fake", _DEFAULT_META), sample_jwt_config
        )
        assert result.success is True
        assert result.ref == "env-new"
        assert result.metadata["status"] == "sent"
        mock_client.create_envelope.assert_called_once()

    def test_envelope_definition_carries_document_base64(
        self, mock_client, sample_jwt_config
    ):
        mock_client.create_envelope.return_value = MagicMock(envelope_id="env-new")
        DocusignSink().push(
            _envelope(b"%PDF-fake", _DEFAULT_META), sample_jwt_config
        )
        envelope_def = mock_client.create_envelope.call_args.args[0]
        assert envelope_def.email_subject == "Please sign the contract"
        assert envelope_def.email_blurb == "Sign at your convenience."
        assert envelope_def.status == "sent"
        assert len(envelope_def.documents) == 1
        assert envelope_def.documents[0].name == "contract.pdf"
        decoded = base64.b64decode(envelope_def.documents[0].document_base64)
        assert decoded == b"%PDF-fake"

    def test_signers_constructed_with_sign_here_tab(
        self, mock_client, sample_jwt_config
    ):
        mock_client.create_envelope.return_value = MagicMock(envelope_id="x")
        DocusignSink().push(
            _envelope(b"%PDF-fake", _DEFAULT_META), sample_jwt_config
        )
        envelope_def = mock_client.create_envelope.call_args.args[0]
        signers = envelope_def.recipients.signers
        assert len(signers) == 2
        assert signers[0].email == "alice@example.com"
        assert signers[0].name == "Alice"
        assert signers[0].recipient_id == "1"
        assert signers[1].recipient_id == "2"
        # Each signer has at least one sign-here tab
        assert len(signers[0].tabs.sign_here_tabs) == 1

    def test_status_param_overrides_default(self, mock_client, sample_jwt_config):
        mock_client.create_envelope.return_value = MagicMock(envelope_id="x", status="created")
        result = DocusignSink().push(
            _envelope(b"%PDF-fake", _DEFAULT_META),
            sample_jwt_config,
            PushParams(params={"status": "created"}),
        )
        envelope_def = mock_client.create_envelope.call_args.args[0]
        assert envelope_def.status == "created"
        assert result.metadata["status"] == "created"

    def test_default_subject_when_not_provided(self, mock_client, sample_jwt_config):
        mock_client.create_envelope.return_value = MagicMock(envelope_id="x")
        meta = {k: v for k, v in _DEFAULT_META.items() if k != "subject"}
        DocusignSink().push(_envelope(b"%PDF-fake", meta), sample_jwt_config)
        envelope_def = mock_client.create_envelope.call_args.args[0]
        assert envelope_def.email_subject == "Please sign"


class TestPushDataCoercion:
    def test_bytes_data(self, mock_client, sample_jwt_config):
        mock_client.create_envelope.return_value = MagicMock(envelope_id="x")
        result = DocusignSink().push(
            _envelope(b"%PDF-fake", _DEFAULT_META), sample_jwt_config
        )
        assert result.success is True

    def test_stream_data(self, mock_client, sample_jwt_config):
        mock_client.create_envelope.return_value = MagicMock(envelope_id="x")
        stream = io.BytesIO(b"%PDF-stream")
        env = DataEnvelope(
            content_type="application/pdf",
            data=b"",
            source_id="t",
            metadata=_DEFAULT_META,
            stream=stream,
        )
        DocusignSink().push(env, sample_jwt_config)
        envelope_def = mock_client.create_envelope.call_args.args[0]
        decoded = base64.b64decode(envelope_def.documents[0].document_base64)
        assert decoded == b"%PDF-stream"

    def test_str_data_encoded(self, mock_client, sample_jwt_config):
        mock_client.create_envelope.return_value = MagicMock(envelope_id="x")
        result = DocusignSink().push(
            _envelope("not-really-pdf-but-still-bytes", _DEFAULT_META),
            sample_jwt_config,
        )
        assert result.success is True

    def test_unsupported_data_type_returns_failure(self, sample_jwt_config):
        # No DocusignClient mock here — the failure must happen *before* construction
        result = DocusignSink().push(
            _envelope(12345, _DEFAULT_META), sample_jwt_config
        )
        assert result.success is False
        assert "Unsupported data type" in result.error


class TestPushErrorPaths:
    def test_missing_filename_returns_failure(self, sample_jwt_config):
        meta = {k: v for k, v in _DEFAULT_META.items() if k != "filename"}
        result = DocusignSink().push(_envelope(b"%PDF", meta), sample_jwt_config)
        assert result.success is False
        assert "filename" in result.error

    def test_missing_signers_returns_failure(self, sample_jwt_config):
        meta = {k: v for k, v in _DEFAULT_META.items() if k != "signers"}
        result = DocusignSink().push(_envelope(b"%PDF", meta), sample_jwt_config)
        assert result.success is False
        assert "signers" in result.error

    def test_sdk_exception_returns_failure(self, mock_client, sample_jwt_config):
        mock_client.create_envelope.side_effect = RuntimeError("boom")
        result = DocusignSink().push(
            _envelope(b"%PDF", _DEFAULT_META), sample_jwt_config
        )
        assert result.success is False
        assert "boom" in result.error

    def test_compliance_empty_envelope_returns_push_result(self, sample_jwt_config):
        """Compliance: push() must return PushResult even for garbage input."""
        result = DocusignSink().push(
            DataEnvelope(content_type="text/plain", data="test", source_id="t"),
            sample_jwt_config,
        )
        assert isinstance(result, PushResult)
        assert result.success is False
