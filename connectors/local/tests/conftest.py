"""Shared pytest fixtures for local filesystem connector tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import create_test_tree


@pytest.fixture
def sample_config(tmp_path) -> dict:
    return {"base_path": str(tmp_path)}


@pytest.fixture
def populated_dir(tmp_path) -> Path:
    """Create a tmp directory with a few test files and subdirectories."""
    create_test_tree(
        tmp_path,
        {
            "a.txt": b"content-a",
            "b.pdf": b"%PDF-fake-content",
            "sub/c.txt": b"content-c",
            "sub/deep/d.csv": "col1,col2\n1,2\n",
        },
    )
    # Also create an empty subdirectory
    (tmp_path / "empty_dir").mkdir()
    return tmp_path


@pytest.fixture
def populated_config(populated_dir) -> dict:
    return {"base_path": str(populated_dir)}
