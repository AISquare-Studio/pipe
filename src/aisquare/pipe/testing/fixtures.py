"""Test fixtures: mock envelopes, configs, and connectors."""

from __future__ import annotations

from aisquare.pipe.core.envelope import DataEnvelope


def make_text_envelope(
    text: str = "Hello, world!",
    source_id: str = "test",
    **metadata: object,
) -> DataEnvelope:
    """Create a text/plain envelope for testing."""
    return DataEnvelope(
        content_type="text/plain",
        data=text,
        source_id=source_id,
        metadata=dict(metadata),
    )


def make_json_envelope(
    data: dict | None = None,
    source_id: str = "test",
    **metadata: object,
) -> DataEnvelope:
    """Create an application/json envelope for testing."""
    return DataEnvelope(
        content_type="application/json",
        data=data or {"key": "value"},
        source_id=source_id,
        metadata=dict(metadata),
    )


def make_binary_envelope(
    data: bytes = b"\x89PNG\r\n\x1a\n",
    content_type: str = "image/png",
    source_id: str = "test",
    **metadata: object,
) -> DataEnvelope:
    """Create a binary envelope for testing."""
    return DataEnvelope(
        content_type=content_type,
        data=data,
        source_id=source_id,
        metadata=dict(metadata),
    )


def make_mock_config() -> dict:
    """Create a minimal mock config dict."""
    return {"mock-source": {}, "mock-sink": {}}
