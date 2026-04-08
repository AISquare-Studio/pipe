"""Mock connectors for testing the aisquare.pipe framework."""

from __future__ import annotations

from collections.abc import Iterator

from aisquare.pipe.core.connector import AuthType, SinkConnector, SourceConnector
from aisquare.pipe.core.envelope import (
    DataEnvelope,
    MetaField,
    PullParams,
    PushParams,
    PushResult,
)
from aisquare.pipe.core.types import TypeConverter


class MockSource(SourceConnector):
    """Generates fake envelopes for testing."""

    name = "mock-source"
    version = "0.1.0"
    output_types = ["text/plain", "application/json", "image/png"]
    auth_type = AuthType.NONE
    description = "Mock source connector for testing"
    metadata_spec = {
        "filename": MetaField(type=str, required=False, description="Filename"),
        "index": MetaField(type=int, required=False, description="Item index"),
    }

    def __init__(
        self,
        envelopes: list[DataEnvelope] | None = None,
        count: int = 3,
    ) -> None:
        self._envelopes = envelopes
        self._count = count

    def pull(
        self, config: dict, params: PullParams | None = None
    ) -> Iterator[DataEnvelope]:
        if self._envelopes is not None:
            yield from self._envelopes
        else:
            for i in range(self._count):
                yield DataEnvelope(
                    content_type="text/plain",
                    data=f"Mock data item {i}",
                    source_id="mock-source",
                    metadata={"filename": f"item_{i}.txt", "index": i},
                )

    def validate_config(self, config: dict) -> bool:
        return True


class MockSink(SinkConnector):
    """Collects envelopes in memory for inspection."""

    name = "mock-sink"
    version = "0.1.0"
    input_types = ["text/plain", "application/json"]
    auth_type = AuthType.NONE
    description = "Mock sink connector for testing"

    def __init__(self) -> None:
        self.received: list[DataEnvelope] = []

    def push(
        self,
        envelope: DataEnvelope,
        config: dict,
        params: PushParams | None = None,
    ) -> PushResult:
        self.received.append(envelope)
        return PushResult(success=True, ref=f"mock-{len(self.received)}")

    def validate_config(self, config: dict) -> bool:
        return True


class MockConverter(TypeConverter):
    """Converts text/plain to application/json by wrapping text in {"text": ...}."""

    from_type = "text/plain"
    to_type = "application/json"

    def convert(self, envelope: DataEnvelope) -> DataEnvelope:
        text = (
            envelope.data
            if isinstance(envelope.data, str)
            else envelope.data.decode() if isinstance(envelope.data, bytes) else str(envelope.data)
        )
        return DataEnvelope(
            content_type="application/json",
            data={"text": text},
            metadata=envelope.metadata.copy(),
            source_id=envelope.source_id,
        )
