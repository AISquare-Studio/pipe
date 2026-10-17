"""Shared pytest fixtures for OneDrive connector tests."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from aisquare.pipe.testing.validation import no_network


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


@pytest.fixture(autouse=True)
def _hermetic(request):
    """Every unit test runs under core's socket guard: any INET socket,
    connect or DNS lookup is recorded, blocked, and fails the test. Live
    tests (``-m live``) are exempt."""
    if request.node.get_closest_marker("live"):
        yield
        return
    with no_network() as log:
        yield
    assert not log.attempts, f"unit test touched the network: {log.attempts}"
