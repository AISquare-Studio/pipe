"""Tests for the DocuSign SDK wrapper and auth resolution."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from aisquare.pipe.errors import ConfigValidationError, PipelineError

from aisquare_pipe_docusign.auth import build_client, has_valid_auth_keys
from aisquare_pipe_docusign.client import DocusignClient
from aisquare_pipe_docusign.constants import DOCUSIGN_SCOPES, JWT_EXPIRES_IN


class TestHasValidAuthKeys:
    def test_jwt_keys_present(self, sample_jwt_config):
        assert has_valid_auth_keys(sample_jwt_config) is True

    def test_auth_code_keys_present(self, sample_auth_code_config):
        assert has_valid_auth_keys(sample_auth_code_config) is True

    def test_partial_jwt_rejected(self):
        assert has_valid_auth_keys({"integration_key": "x", "user_id": "y"}) is False

    def test_partial_auth_code_rejected(self):
        assert has_valid_auth_keys({"client_id": "x", "refresh_token": "y"}) is False

    def test_empty_rejected(self):
        assert has_valid_auth_keys({}) is False


class TestBuildClientJWT:
    def test_calls_request_jwt_user_token(self, mock_api_client, sample_jwt_config):
        build_client(sample_jwt_config)
        mock_api_client.request_jwt_user_token.assert_called_once()
        kwargs = mock_api_client.request_jwt_user_token.call_args.kwargs
        assert kwargs["client_id"] == sample_jwt_config["integration_key"]
        assert kwargs["user_id"] == sample_jwt_config["user_id"]
        assert kwargs["oauth_host_name"] == sample_jwt_config["auth_server"]
        assert isinstance(kwargs["private_key_bytes"], bytes)
        assert kwargs["expires_in"] == JWT_EXPIRES_IN
        assert kwargs["scopes"] == DOCUSIGN_SCOPES

    def test_sets_bearer_header(self, mock_api_client, sample_jwt_config):
        build_client(sample_jwt_config)
        assert (
            mock_api_client.default_headers["Authorization"]
            == "Bearer access-token-fresh"
        )

    def test_discovers_account_and_sets_host(self, mock_api_client, sample_jwt_config):
        api_client, account_id = build_client(sample_jwt_config)
        assert account_id == "account-xyz"
        assert api_client.host == "https://demo.docusign.net/restapi"

    def test_private_key_as_bytes_accepted(self, mock_api_client, sample_jwt_config):
        cfg = {**sample_jwt_config, "private_key": b"raw-bytes"}
        build_client(cfg)
        assert (
            mock_api_client.request_jwt_user_token.call_args.kwargs["private_key_bytes"]
            == b"raw-bytes"
        )


class TestBuildClientAuthCode:
    def test_calls_refresh_endpoint(
        self, mock_api_client, mock_requests_post, sample_auth_code_config
    ):
        build_client(sample_auth_code_config)
        called_url = mock_requests_post.call_args.args[0]
        assert called_url == "https://account-d.docusign.com/oauth/token"
        payload = mock_requests_post.call_args.kwargs["data"]
        assert payload["grant_type"] == "refresh_token"
        assert payload["refresh_token"] == "refresh-tok"
        assert payload["client_id"] == "ikey-abc"
        assert payload["client_secret"] == "secret-xyz"

    def test_uses_refreshed_token_for_header(
        self, mock_api_client, mock_requests_post, sample_auth_code_config
    ):
        build_client(sample_auth_code_config)
        assert (
            mock_api_client.default_headers["Authorization"]
            == "Bearer refreshed-access-token"
        )

    def test_refresh_failure_raises(self, mock_api_client, sample_auth_code_config):
        with patch("aisquare_pipe_docusign.auth.requests.post") as mock_post:
            mock_post.return_value.status_code = 400
            mock_post.return_value.text = "invalid_grant"
            with pytest.raises(ConfigValidationError):
                build_client(sample_auth_code_config)


class TestBuildClientMisc:
    def test_missing_credentials_raises(self):
        with pytest.raises(ConfigValidationError):
            build_client({})

    def test_account_id_override_skips_discovery_call(
        self, mock_api_client, sample_jwt_config
    ):
        cfg = {
            **sample_jwt_config,
            "account_id": "explicit-account",
            "base_uri": "https://overridden.docusign.net",
        }
        _, account_id = build_client(cfg)
        assert account_id == "explicit-account"
        mock_api_client.get_user_info.assert_not_called()


class TestDocusignClient:
    def test_validate_calls_list_status_changes(
        self, mock_api_client, mock_envelopes_api, mock_folders_api, sample_jwt_config
    ):
        client = DocusignClient(sample_jwt_config)
        assert client.validate() is True
        mock_envelopes_api.list_status_changes.assert_called_once()
        assert client.account_id == "account-xyz"

    def test_list_envelopes_paginates(
        self, mock_api_client, mock_envelopes_api, mock_folders_api, sample_jwt_config
    ):
        from unittest.mock import MagicMock

        page1 = MagicMock()
        page1.envelopes = [MagicMock(envelope_id="e1"), MagicMock(envelope_id="e2")]
        page1.next_uri = "/next"
        page2 = MagicMock()
        page2.envelopes = [MagicMock(envelope_id="e3")]
        page2.next_uri = None
        mock_envelopes_api.list_status_changes.side_effect = [page1, page2]

        client = DocusignClient(sample_jwt_config)
        ids = [e.envelope_id for e in client.list_envelopes(from_date="2024-01-01")]
        assert ids == ["e1", "e2", "e3"]
        assert mock_envelopes_api.list_status_changes.call_count == 2

    def test_list_envelopes_respects_limit(
        self, mock_api_client, mock_envelopes_api, mock_folders_api, sample_jwt_config
    ):
        from unittest.mock import MagicMock

        page = MagicMock()
        page.envelopes = [MagicMock(envelope_id=f"e{i}") for i in range(5)]
        page.next_uri = None
        mock_envelopes_api.list_status_changes.return_value = page

        client = DocusignClient(sample_jwt_config)
        ids = [e.envelope_id for e in client.list_envelopes(limit=3)]
        assert ids == ["e0", "e1", "e2"]

    def test_get_document_bytes_reads_temp_file(
        self,
        tmp_path,
        mock_api_client,
        mock_envelopes_api,
        mock_folders_api,
        sample_jwt_config,
    ):
        temp_pdf = tmp_path / "doc.pdf"
        temp_pdf.write_bytes(b"%PDF-1.4 fake content")
        mock_envelopes_api.get_document.return_value = str(temp_pdf)

        client = DocusignClient(sample_jwt_config)
        data = client.get_document_bytes("env-1", "1")
        assert data == b"%PDF-1.4 fake content"
        # temp file should be cleaned up
        assert not temp_pdf.exists()

    def test_create_envelope_dispatches(
        self, mock_api_client, mock_envelopes_api, mock_folders_api, sample_jwt_config
    ):
        from unittest.mock import MagicMock

        envelope_definition = MagicMock()
        mock_envelopes_api.create_envelope.return_value = MagicMock(envelope_id="new-env")

        client = DocusignClient(sample_jwt_config)
        result = client.create_envelope(envelope_definition)
        assert result.envelope_id == "new-env"
        mock_envelopes_api.create_envelope.assert_called_once_with(
            "account-xyz", envelope_definition=envelope_definition
        )

    def test_api_exception_401_mapped_to_config_error(
        self, mock_api_client, mock_envelopes_api, mock_folders_api, sample_jwt_config
    ):
        from docusign_esign.client.api_exception import ApiException

        exc = ApiException(status=401, reason="Unauthorized")
        exc.trace_token = "trace-1"
        exc.timestamp = "2024-01-01T00:00:00Z"
        exc.body = "{}"
        mock_envelopes_api.list_status_changes.side_effect = exc
        client = DocusignClient(sample_jwt_config)
        with pytest.raises(ConfigValidationError):
            client.validate()

    def test_api_exception_500_mapped_to_pipeline_error(
        self, mock_api_client, mock_envelopes_api, mock_folders_api, sample_jwt_config
    ):
        from docusign_esign.client.api_exception import ApiException

        exc = ApiException(status=500, reason="Server Error")
        exc.trace_token = "trace-2"
        exc.timestamp = "2024-01-01T00:00:00Z"
        exc.body = "{}"
        mock_envelopes_api.list_status_changes.side_effect = exc
        client = DocusignClient(sample_jwt_config)
        with pytest.raises(PipelineError):
            client.validate()
