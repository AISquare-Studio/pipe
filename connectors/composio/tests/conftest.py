"""Shared fixtures for the Composio connector tests.

All tests are hermetic: connector/factory/connections/triggers tests mock at
the ComposioClient boundary (patched at each importing module), and client
tests mock the composio SDK class itself.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def composio_config() -> dict:
    return {"api_key": "test-key", "user_id": "test-user"}


def _make_client_mock() -> MagicMock:
    instance = MagicMock()
    instance.download_dir = None
    instance.upload_dir = None
    return instance


@pytest.fixture
def mock_client():
    """ComposioClient mock as seen by connector.py (source, sink, factory)."""
    with patch("aisquare_pipe_composio.connector.ComposioClient") as mock_cls:
        instance = _make_client_mock()
        mock_cls.return_value = instance
        yield instance


@pytest.fixture
def mock_triggers_client():
    """ComposioClient mock as seen by triggers.py."""
    with patch("aisquare_pipe_composio.triggers.ComposioClient") as mock_cls:
        instance = _make_client_mock()
        mock_cls.return_value = instance
        yield instance


@pytest.fixture
def mock_connections_client():
    """ComposioClient mock as seen by connections.py."""
    with patch("aisquare_pipe_composio.connections.ComposioClient") as mock_cls:
        instance = _make_client_mock()
        mock_cls.return_value = instance
        yield instance


@pytest.fixture
def mock_sdk():
    """The composio.Composio class as seen by client.py."""
    with patch("aisquare_pipe_composio.client.Composio") as mock_cls:
        yield mock_cls


@pytest.fixture
def tmp_cursor_path(tmp_path) -> str:
    return str(tmp_path / "cursor.json")
