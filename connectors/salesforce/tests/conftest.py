"""Shared pytest fixtures for Salesforce connector tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.helpers import oauth_config, userpass_config


@pytest.fixture
def sample_userpass_config() -> dict:
    return userpass_config()


@pytest.fixture
def sample_oauth_config() -> dict:
    return oauth_config()


@pytest.fixture
def mock_sf():
    """Patch simple_salesforce.Salesforce in auth.build_client and yield the mock instance."""
    with patch("aisquare_pipe_salesforce.auth.Salesforce") as mock_cls:
        instance = MagicMock()
        mock_cls.return_value = instance
        yield instance


@pytest.fixture
def mock_requests_post():
    """Patch requests.post in auth (used for OAuth refresh)."""
    with patch("aisquare_pipe_salesforce.auth.requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "instance_url": "https://refreshed.my.salesforce.com",
            "access_token": "new-access-token",
        }
        mock_post.return_value = mock_resp
        yield mock_post


@pytest.fixture
def mock_client():
    """Patch SalesforceClient at the connector layer and return the mock instance."""
    with patch("aisquare_pipe_salesforce.connector.SalesforceClient") as mock_cls:
        instance = MagicMock()
        mock_cls.return_value = instance
        yield instance
