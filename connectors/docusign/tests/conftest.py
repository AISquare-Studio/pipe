"""Shared pytest fixtures for DocuSign connector tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.helpers import (
    auth_code_config,
    jwt_config,
    make_oauth_token,
    make_oauth_user_info,
)


@pytest.fixture
def sample_jwt_config() -> dict:
    return jwt_config()


@pytest.fixture
def sample_auth_code_config() -> dict:
    return auth_code_config()


@pytest.fixture
def mock_api_client():
    """Patch docusign_esign.ApiClient in auth.build_client; pre-wire OAuth token + user info."""
    with patch("aisquare_pipe_docusign.auth.ApiClient") as mock_cls:
        instance = MagicMock()
        instance.request_jwt_user_token.return_value = make_oauth_token()
        instance.get_user_info.return_value = make_oauth_user_info()
        instance.default_headers = {}
        mock_cls.return_value = instance
        yield instance


@pytest.fixture
def mock_requests_post():
    """Patch requests.post in auth (used for refresh-token exchange)."""
    with patch("aisquare_pipe_docusign.auth.requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "refreshed-access-token",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
        }
        mock_post.return_value = mock_resp
        yield mock_post


@pytest.fixture
def mock_envelopes_api():
    """Patch docusign_esign.EnvelopesApi used by DocusignClient."""
    with patch("aisquare_pipe_docusign.client.EnvelopesApi") as mock_cls:
        instance = MagicMock()
        mock_cls.return_value = instance
        yield instance


@pytest.fixture
def mock_folders_api():
    with patch("aisquare_pipe_docusign.client.FoldersApi") as mock_cls:
        instance = MagicMock()
        mock_cls.return_value = instance
        yield instance


@pytest.fixture
def mock_client():
    """Patch DocusignClient at the connector layer."""
    with patch("aisquare_pipe_docusign.connector.DocusignClient") as mock_cls:
        instance = MagicMock()
        instance.account_id = "account-xyz"
        mock_cls.return_value = instance
        yield instance
