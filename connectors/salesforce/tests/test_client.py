"""Tests for the Salesforce SDK wrapper and auth resolution."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from aisquare.pipe.errors import ConfigValidationError

from aisquare_pipe_salesforce.auth import build_client, has_valid_auth_keys
from aisquare_pipe_salesforce.client import SalesforceClient


class TestHasValidAuthKeys:
    def test_oauth_keys_present(self, sample_oauth_config):
        assert has_valid_auth_keys(sample_oauth_config) is True

    def test_userpass_keys_present(self, sample_userpass_config):
        assert has_valid_auth_keys(sample_userpass_config) is True

    def test_partial_oauth_rejected(self):
        assert has_valid_auth_keys({"client_id": "x", "refresh_token": "y"}) is False

    def test_partial_userpass_rejected(self):
        assert has_valid_auth_keys({"username": "u", "password": "p"}) is False

    def test_empty_rejected(self):
        assert has_valid_auth_keys({}) is False


class TestBuildClient:
    def test_userpass_flow_constructs_salesforce(self, mock_sf, sample_userpass_config):
        with patch("aisquare_pipe_salesforce.auth.Salesforce") as mock_cls:
            build_client(sample_userpass_config)
            mock_cls.assert_called_once_with(
                username="test@example.com",
                password="secret",
                security_token="tok",
                domain="login",
            )

    def test_userpass_flow_honours_domain(self, sample_userpass_config):
        with patch("aisquare_pipe_salesforce.auth.Salesforce") as mock_cls:
            cfg = {**sample_userpass_config, "domain": "test"}
            build_client(cfg)
            assert mock_cls.call_args.kwargs["domain"] == "test"

    def test_oauth_flow_refreshes_then_constructs(
        self, sample_oauth_config, mock_requests_post
    ):
        with patch("aisquare_pipe_salesforce.auth.Salesforce") as mock_cls:
            build_client(sample_oauth_config)
            # Refresh endpoint called with correct payload
            assert mock_requests_post.call_args.args[0].endswith("/services/oauth2/token")
            payload = mock_requests_post.call_args.kwargs["data"]
            assert payload["grant_type"] == "refresh_token"
            assert payload["refresh_token"] == "ref"
            # Salesforce constructed with instance_url + session_id from refresh response
            mock_cls.assert_called_once_with(
                instance_url="https://refreshed.my.salesforce.com",
                session_id="new-access-token",
            )

    def test_oauth_refresh_failure_raises(self, sample_oauth_config):
        with patch("aisquare_pipe_salesforce.auth.requests.post") as mock_post:
            mock_post.return_value.status_code = 400
            mock_post.return_value.text = "invalid_grant"
            with pytest.raises(ConfigValidationError):
                build_client(sample_oauth_config)

    def test_missing_credentials_raises(self):
        with pytest.raises(ConfigValidationError):
            build_client({})


class TestSalesforceClient:
    def test_validate_calls_limits(self, sample_userpass_config):
        with patch("aisquare_pipe_salesforce.auth.Salesforce") as mock_cls:
            instance = mock_cls.return_value
            client = SalesforceClient(sample_userpass_config)
            assert client.validate() is True
            instance.limits.assert_called_once()

    def test_query_iter_delegates_to_query_all_iter(self, sample_userpass_config):
        with patch("aisquare_pipe_salesforce.auth.Salesforce") as mock_cls:
            instance = mock_cls.return_value
            instance.query_all_iter.return_value = iter([{"Id": "1"}, {"Id": "2"}])
            client = SalesforceClient(sample_userpass_config)
            records = list(client.query_iter("SELECT Id FROM Account"))
            assert records == [{"Id": "1"}, {"Id": "2"}]
            instance.query_all_iter.assert_called_once()

    def test_create_dispatches_to_sobject(self, sample_userpass_config):
        with patch("aisquare_pipe_salesforce.auth.Salesforce") as mock_cls:
            instance = mock_cls.return_value
            instance.Account.create.return_value = {"id": "new123", "success": True}
            client = SalesforceClient(sample_userpass_config)
            result = client.create("Account", {"Name": "Foo"})
            instance.Account.create.assert_called_once_with({"Name": "Foo"})
            assert result["id"] == "new123"

    def test_update_dispatches_to_sobject(self, sample_userpass_config):
        with patch("aisquare_pipe_salesforce.auth.Salesforce") as mock_cls:
            instance = mock_cls.return_value
            instance.Account.update.return_value = 204
            client = SalesforceClient(sample_userpass_config)
            assert client.update("Account", "001ABC", {"Name": "Bar"}) == 204
            instance.Account.update.assert_called_once_with("001ABC", {"Name": "Bar"})

    def test_upsert_dispatches_with_external_id(self, sample_userpass_config):
        with patch("aisquare_pipe_salesforce.auth.Salesforce") as mock_cls:
            instance = mock_cls.return_value
            instance.Account.upsert.return_value = 201
            client = SalesforceClient(sample_userpass_config)
            client.upsert("Account", "Ext__c", "ext-1", {"Name": "Baz"})
            instance.Account.upsert.assert_called_once_with(
                "Ext__c/ext-1", {"Name": "Baz"}
            )
