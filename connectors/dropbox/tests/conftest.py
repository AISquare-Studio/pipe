"""Shared pytest fixtures for Dropbox connector tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.helpers import make_file_metadata


@pytest.fixture
def sample_config() -> dict:
    return {"access_token": "test-token-abc"}


@pytest.fixture
def sample_config_refresh() -> dict:
    return {
        "app_key": "test-app-key",
        "app_secret": "test-app-secret",
        "refresh_token": "test-refresh-token",
    }


@pytest.fixture
def sample_file_metadata():
    return make_file_metadata()


@pytest.fixture
def mock_dbx():
    """Patch dropbox.Dropbox and return the mock instance."""
    with patch("aisquare_pipe_dropbox.client.dropbox.Dropbox") as mock_cls:
        mock_instance = MagicMock()
        mock_cls.return_value = mock_instance
        yield mock_instance
