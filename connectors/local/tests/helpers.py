"""Test helpers for local filesystem connector tests."""

from __future__ import annotations

from pathlib import Path


def create_test_tree(base: Path, files: dict[str, bytes | str]) -> None:
    """Create a directory tree with the given files.

    Args:
        base: root directory
        files: mapping of relative paths to contents.
               str values are encoded as UTF-8, bytes are written directly.

    Example:
        create_test_tree(tmp_path, {
            "a.txt": b"hello",
            "sub/b.pdf": b"%PDF-fake",
            "sub/deep/c.csv": "col1,col2",
        })
    """
    for rel_path, content in files.items():
        full = base / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            full.write_text(content, encoding="utf-8")
        else:
            full.write_bytes(content)


def create_test_file(base: Path, name: str, size: int = 100) -> Path:
    """Create a single test file filled with zero bytes."""
    path = base / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * size)
    return path
