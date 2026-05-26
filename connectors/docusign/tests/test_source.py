"""Tests for DocusignSource."""

from __future__ import annotations

import pytest

from aisquare.pipe.core.envelope import PullParams, RateLimit

from aisquare_pipe_docusign.connector import DocusignSource

from tests.helpers import make_document, make_envelope


class TestDocusignSourceDocumentsMode:
    def test_yields_one_envelope_per_document(self, mock_client, sample_jwt_config):
        env = make_envelope("env-1")
        docs = [make_document("1", "contract.pdf"), make_document("2", "exhibit-a.pdf")]
        mock_client.list_envelopes.return_value = iter([env])
        mock_client.list_documents.return_value = docs
        mock_client.get_document_bytes.side_effect = [b"%PDF-doc1", b"%PDF-doc2"]

        params = PullParams(params={"from_date": "2024-01-01"})
        envelopes = list(DocusignSource().pull(sample_jwt_config, params))

        assert len(envelopes) == 2
        assert envelopes[0].content_type == "application/pdf"
        assert envelopes[0].data == b"%PDF-doc1"
        assert envelopes[0].metadata["envelope_id"] == "env-1"
        assert envelopes[0].metadata["document_id"] == "1"
        assert envelopes[0].metadata["filename"] == "contract.pdf"
        assert envelopes[1].metadata["filename"] == "exhibit-a.pdf"
        assert envelopes[0].source_id == "docusign-source"

    def test_skips_combined_pdf_by_default(self, mock_client, sample_jwt_config):
        env = make_envelope("env-1")
        docs = [make_document("1", "doc.pdf"), make_document("combined", "combined.pdf")]
        mock_client.list_envelopes.return_value = iter([env])
        mock_client.list_documents.return_value = docs
        mock_client.get_document_bytes.return_value = b"data"

        envelopes = list(DocusignSource().pull(sample_jwt_config, PullParams()))
        assert len(envelopes) == 1
        assert envelopes[0].metadata["document_id"] == "1"

    def test_include_combined_true(self, mock_client, sample_jwt_config):
        env = make_envelope("env-1")
        docs = [make_document("1"), make_document("combined")]
        mock_client.list_envelopes.return_value = iter([env])
        mock_client.list_documents.return_value = docs
        mock_client.get_document_bytes.return_value = b"data"

        params = PullParams(params={"include_combined": True})
        envelopes = list(DocusignSource().pull(sample_jwt_config, params))
        assert len(envelopes) == 2

    def test_filters_propagated_to_client(self, mock_client, sample_jwt_config):
        mock_client.list_envelopes.return_value = iter([])
        params = PullParams(params={
            "status": "completed",
            "from_date": "2024-01-01",
            "to_date": "2024-12-31",
            "folder_id": "fld-1",
            "envelope_ids": ["env-a", "env-b"],
            "limit": 10,
        })
        list(DocusignSource().pull(sample_jwt_config, params))
        kwargs = mock_client.list_envelopes.call_args.kwargs
        assert kwargs["status"] == "completed"
        assert kwargs["from_date"] == "2024-01-01"
        assert kwargs["to_date"] == "2024-12-31"
        assert kwargs["folder_id"] == "fld-1"
        assert kwargs["envelope_ids"] == ["env-a", "env-b"]
        assert kwargs["limit"] == 10


class TestDocusignSourceEnvelopesMode:
    def test_yields_json_envelopes(self, mock_client, sample_jwt_config):
        env = make_envelope("env-9", status="completed", subject="Signed!")
        mock_client.list_envelopes.return_value = iter([env])

        params = PullParams(params={"mode": "envelopes"})
        envelopes = list(DocusignSource().pull(sample_jwt_config, params))

        assert len(envelopes) == 1
        assert envelopes[0].content_type == "application/json"
        assert isinstance(envelopes[0].data, dict)
        assert envelopes[0].metadata["envelope_id"] == "env-9"
        assert envelopes[0].metadata["status"] == "completed"
        # In envelopes mode we do NOT call list_documents or get_document_bytes
        mock_client.list_documents.assert_not_called()
        mock_client.get_document_bytes.assert_not_called()


class TestDocusignSourceMisc:
    def test_unknown_mode_raises(self, mock_client, sample_jwt_config):
        params = PullParams(params={"mode": "bogus"})
        gen = DocusignSource().pull(sample_jwt_config, params)
        with pytest.raises(ValueError):
            next(gen)

    def test_rate_limit_returned(self):
        rl = DocusignSource().rate_limit()
        assert isinstance(rl, RateLimit)
        assert rl.requests_per_second == 3

    def test_validate_config_empty_returns_false(self):
        assert DocusignSource().validate_config({}) is False

    def test_validate_config_happy(self, mock_client, sample_jwt_config):
        mock_client.validate.return_value = True
        assert DocusignSource().validate_config(sample_jwt_config) is True

    def test_validate_config_swallows_exceptions(self, sample_jwt_config):
        from unittest.mock import patch
        with patch("aisquare_pipe_docusign.connector.DocusignClient") as mock_cls:
            mock_cls.side_effect = Exception("connection failed")
            assert DocusignSource().validate_config(sample_jwt_config) is False

    def test_list_resources_returns_folders(self, mock_client, sample_jwt_config):
        from unittest.mock import MagicMock

        folder = MagicMock()
        folder.folder_id = "fld-1"
        folder.name = "Inbox"
        folder.type = "inbox"
        folder.owner_email = "me@example.com"
        mock_client.list_folders.return_value = [folder]
        resources = DocusignSource().list_resources(sample_jwt_config)
        assert len(resources) == 1
        assert resources[0].id == "fld-1"
        assert resources[0].name == "Inbox"
        assert resources[0].resource_type == "folder"
