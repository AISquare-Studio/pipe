"""aisquare.pipe — Universal anything-to-anything connector framework."""

from aisquare.pipe.core.connector import (
    AuthType,
    DuplexConnector,
    SinkConnector,
    SourceConnector,
)
from aisquare.pipe.core.envelope import (
    DataEnvelope,
    MetaField,
    PullParams,
    PushParams,
    PushResult,
    RateLimit,
    Resource,
)
from aisquare.pipe.core.merge import MergeStrategy
from aisquare.pipe.core.pipeline import CompatibilityReport, Pipeline, PipelineResult
from aisquare.pipe.core.registry import (
    discover_connectors,
    discover_converters,
    get_connector,
    get_converter,
)
from aisquare.pipe.core.types import MatchLevel, MatchResult, TypeConverter, TypeMatcher
from aisquare.pipe.errors import (
    ConfigValidationError,
    ConnectorNotFoundError,
    ConverterError,
    EnvelopeValidationError,
    PipeError,
    PipelineError,
    TypeMismatchError,
)

__all__ = [
    # Envelope & types
    "DataEnvelope",
    "MetaField",
    "PullParams",
    "PushParams",
    "PushResult",
    "Resource",
    "RateLimit",
    # Connectors
    "SourceConnector",
    "SinkConnector",
    "DuplexConnector",
    "AuthType",
    # Pipeline
    "Pipeline",
    "PipelineResult",
    "CompatibilityReport",
    # Type system
    "TypeMatcher",
    "TypeConverter",
    "MatchResult",
    "MatchLevel",
    # Merge
    "MergeStrategy",
    # Registry
    "discover_connectors",
    "discover_converters",
    "get_connector",
    "get_converter",
    # Errors
    "PipeError",
    "ConnectorNotFoundError",
    "ConfigValidationError",
    "TypeMismatchError",
    "EnvelopeValidationError",
    "PipelineError",
    "ConverterError",
]
