"""Shared pytest fixtures for Google Drive connector tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def sample_config() -> dict:
    return {"access_token": "test-token-abc"}


@pytest.fixture
def sample_config_refresh() -> dict:
    return {
        "client_id": "test-client-id",
        "client_secret": "test-client-secret",
        "refresh_token": "test-refresh-token",
    }


@pytest.fixture
def sample_service_account() -> dict:
    return {
        "type": "service_account",
        "project_id": "test-project",
        "private_key_id": "key-id",
        "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
        "client_email": "svc@test-project.iam.gserviceaccount.com",
        "token_uri": "https://oauth2.googleapis.com/token",
    }


@pytest.fixture
def mock_session():
    """Patch AuthorizedSession so no credentials or network are needed."""
    with patch("aisquare_pipe_gdrive.client.AuthorizedSession") as cls:
        session = MagicMock()
        cls.return_value = session
        yield session
