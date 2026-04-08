"""DataEnvelope and supporting types for aisquare.pipe."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, IO


@dataclass
class MetaField:
    """Describes a metadata field in a connector's metadata_spec."""

    type: type
    required: bool = False
    description: str = ""
    max_length: int | None = None
    default: Any = None


@dataclass
class PullParams:
    """Container for source-specific pull parameters."""

    params: dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        return self.params[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.params[key] = value

    def __contains__(self, key: str) -> bool:
        return key in self.params

    def get(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)


@dataclass
class PushParams:
    """Container for sink-specific push parameters."""

    params: dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        return self.params[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.params[key] = value

    def __contains__(self, key: str) -> bool:
        return key in self.params

    def get(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)


@dataclass
class PushResult:
    """Result of pushing an envelope to a sink."""

    success: bool
    ref: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Resource:
    """Represents a browsable resource at a source."""

    id: str
    name: str
    resource_type: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RateLimit:
    """Rate limiting configuration for a connector."""

    requests_per_second: float | None = None
    requests_per_minute: float | None = None
    concurrent: int | None = None


@dataclass
class DataEnvelope:
    """Universal data container passed between connectors.

    Every piece of data flowing through a pipeline is wrapped in a DataEnvelope.
    """

    content_type: str
    data: bytes | str | dict[str, Any]
    source_id: str
    metadata: dict[str, Any] = field(default_factory=dict)
    schema: dict[str, Any] | None = None
    stream: IO[bytes] | None = None

    def size(self) -> int | None:
        """Return size in bytes if determinable."""
        if isinstance(self.data, bytes):
            return len(self.data)
        if isinstance(self.data, str):
            return len(self.data.encode("utf-8"))
        return None
