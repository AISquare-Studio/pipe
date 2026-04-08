"""Abstract base classes for source and sink connectors."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from enum import Enum

from aisquare.pipe.core.envelope import (
    DataEnvelope,
    MetaField,
    PullParams,
    PushParams,
    PushResult,
    RateLimit,
    Resource,
)


class AuthType(Enum):
    """Authentication type required by a connector."""

    NONE = "none"
    API_KEY = "api_key"
    OAUTH2 = "oauth2"
    CUSTOM = "custom"


class SourceConnector(ABC):
    """Base class for all source connectors."""

    name: str
    version: str
    output_types: list[str]
    auth_type: AuthType

    description: str = ""
    docs_url: str = ""
    metadata_spec: dict[str, MetaField] = {}

    @abstractmethod
    def pull(
        self, config: dict, params: PullParams | None = None
    ) -> Iterator[DataEnvelope]:
        """Yield envelopes from the source. Must be a generator/iterator."""
        ...

    @abstractmethod
    def validate_config(self, config: dict) -> bool:
        """Return True if the config/credentials are valid."""
        ...

    def list_resources(self, config: dict) -> list[Resource]:
        """Optional: browse available items at this source."""
        raise NotImplementedError(
            f"{self.name} does not support resource listing"
        )

    def supports_streaming(self) -> bool:
        return False

    def rate_limit(self) -> RateLimit | None:
        return None


class SinkConnector(ABC):
    """Base class for all sink connectors."""

    name: str
    version: str
    input_types: list[str]
    auth_type: AuthType

    description: str = ""
    docs_url: str = ""
    metadata_spec: dict[str, MetaField] = {}

    @abstractmethod
    def push(
        self,
        envelope: DataEnvelope,
        config: dict,
        params: PushParams | None = None,
    ) -> PushResult:
        """Push an envelope to the sink."""
        ...

    @abstractmethod
    def validate_config(self, config: dict) -> bool:
        """Return True if the config/credentials are valid."""
        ...

    def accepts(self, envelope: DataEnvelope) -> bool:
        """Fine-grained check. Default: check content_type against input_types."""
        return self._type_matches(envelope.content_type)

    def _type_matches(self, content_type: str) -> bool:
        """Check if content_type matches any input_type (supports wildcards)."""
        for accepted in self.input_types:
            if accepted == "*/*":
                return True
            if accepted == content_type:
                return True
            if accepted.endswith("/*"):
                prefix = accepted.split("/")[0]
                if content_type.startswith(prefix + "/"):
                    return True
        return False

    def max_size(self) -> int | None:
        return None


class DuplexConnector(SourceConnector, SinkConnector):
    """For connectors that are both source and sink (e.g., local filesystem, S3)."""

    pass
