"""Shared pytest fixtures for OneDrive connector tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def sample_config() -> dict:
    return {"access_token": "test-token-abc"}


@pytest.fixture
def sample_config_client_creds() -> dict:
    return {
        "client_id": "test-client-id",
        "client_secret": "test-client-secret",
        "tenant_id": "test-tenant-id",
    }


@pytest.fixture
def mock_requests():
    """Patch requests.request and return the mock."""
    with patch("aisquare_pipe_onedrive.client.requests.request") as mock_req:
        yield mock_req
