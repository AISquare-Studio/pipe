"""Unit tests for connected-account lifecycle helpers."""

from __future__ import annotations

import pytest

from aisquare.pipe.errors import ConfigValidationError

from aisquare_pipe_composio.connections import (
    connection_status,
    initiate_connection,
    list_connections,
    wait_for_active,
)

from tests.helpers import make_account


class TestInitiateConnection:
    def test_explicit_auth_config_id(self, mock_connections_client, composio_config):
        mock_connections_client.initiate_connection.return_value = {
            "id": "conn_1",
            "status": "INITIATED",
            "redirect_url": "https://auth.example/redirect",
        }
        request = initiate_connection(
            composio_config, "gmail", auth_config_id="ac_7", callback_url="https://cb"
        )
        assert request.id == "conn_1"
        assert request.redirect_url == "https://auth.example/redirect"
        assert request.status == "INITIATED"
        mock_connections_client.initiate_connection.assert_called_once_with(
            user_id="test-user", auth_config_id="ac_7", callback_url="https://cb"
        )
        mock_connections_client.list_auth_configs.assert_not_called()

    def test_auto_resolves_single_auth_config(
        self, mock_connections_client, composio_config
    ):
        mock_connections_client.list_auth_configs.return_value = [{"id": "ac_1"}]
        mock_connections_client.initiate_connection.return_value = {
            "id": "conn_2",
            "status": "INITIATED",
            "redirect_url": None,
        }
        request = initiate_connection(composio_config, "gmail")
        assert request.id == "conn_2"
        assert (
            mock_connections_client.initiate_connection.call_args.kwargs[
                "auth_config_id"
            ]
            == "ac_1"
        )

    def test_multiple_auth_configs_raise(
        self, mock_connections_client, composio_config
    ):
        mock_connections_client.list_auth_configs.return_value = [
            {"id": "ac_1"},
            {"id": "ac_2"},
        ]
        with pytest.raises(ConfigValidationError, match="auth_config_id"):
            initiate_connection(composio_config, "gmail")

    def test_no_auth_config_falls_back_to_authorize(
        self, mock_connections_client, composio_config
    ):
        mock_connections_client.list_auth_configs.return_value = []
        mock_connections_client.authorize_toolkit.return_value = {
            "id": "conn_3",
            "status": "INITIATED",
            "redirect_url": "https://auth",
        }
        request = initiate_connection(composio_config, "notion")
        assert request.id == "conn_3"
        mock_connections_client.authorize_toolkit.assert_called_once_with(
            user_id="test-user", toolkit="notion"
        )

    def test_missing_request_id_raises(
        self, mock_connections_client, composio_config
    ):
        mock_connections_client.list_auth_configs.return_value = []
        mock_connections_client.authorize_toolkit.return_value = {"id": None}
        with pytest.raises(ConfigValidationError, match="connection request id"):
            initiate_connection(composio_config, "gmail")


class TestWaitAndList:
    def test_wait_for_active_delegates(self, mock_connections_client, composio_config):
        mock_connections_client.wait_for_connection.return_value = {"status": "ACTIVE"}
        account = wait_for_active(composio_config, "conn_1", timeout=10)
        assert account == {"status": "ACTIVE"}
        mock_connections_client.wait_for_connection.assert_called_once_with(
            "conn_1", timeout=10
        )

    def test_list_connections_scopes_to_user_and_toolkit(
        self, mock_connections_client, composio_config
    ):
        mock_connections_client.list_connected_accounts.return_value = [make_account()]
        accounts = list_connections(composio_config, toolkit="gmail")
        assert len(accounts) == 1
        mock_connections_client.list_connected_accounts.assert_called_once_with(
            user_id="test-user", toolkits=["gmail"]
        )


class TestConnectionStatus:
    def test_not_connected(self, mock_connections_client, composio_config):
        mock_connections_client.list_connected_accounts.return_value = []
        assert connection_status(composio_config, "gmail") == "NOT_CONNECTED"

    def test_active_wins(self, mock_connections_client, composio_config):
        mock_connections_client.list_connected_accounts.return_value = [
            make_account(status="EXPIRED"),
            make_account(status="ACTIVE"),
        ]
        assert connection_status(composio_config, "gmail") == "ACTIVE"

    def test_first_status_when_no_active(
        self, mock_connections_client, composio_config
    ):
        mock_connections_client.list_connected_accounts.return_value = [
            make_account(status="INITIATED")
        ]
        assert connection_status(composio_config, "gmail") == "INITIATED"
