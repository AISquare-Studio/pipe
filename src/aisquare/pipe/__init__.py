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
from aisquare.pipe.core.oauth import (
    OAuthSpec,
    OAuthToken,
    build_authorize_url,
    exchange_code,
    make_pkce_pair,
    refresh_access_token,
    revoke_token,
)
from aisquare.pipe.core.pipeline import CompatibilityReport, Pipeline, PipelineResult
from aisquare.pipe.core.registry import (
    discover_connectors,
    discover_converters,
    discover_toolkits,
    get_connector,
    get_converter,
    get_toolkit,
)
from aisquare.pipe.core.tools import (
    ERROR_CODES,
    ERROR_CONFLICT,
    ERROR_FORBIDDEN,
    ERROR_INVALID_ARGUMENTS,
    ERROR_NOT_FOUND,
    ERROR_RATE_LIMITED,
    ERROR_TIMEOUT,
    ERROR_UNAUTHORIZED,
    ERROR_UNKNOWN_TOOL,
    ERROR_UPSTREAM_ERROR,
    Toolkit,
    ToolResult,
    ToolSpec,
    error_code_for_status,
    rank_tools,
    validate_arguments,
)
from aisquare.pipe.core.types import MatchLevel, MatchResult, TypeConverter, TypeMatcher
from aisquare.pipe.errors import (
    ConfigValidationError,
    ConnectorNotFoundError,
    ConverterError,
    EnvelopeValidationError,
    OAuthError,
    PipeError,
    PipelineError,
    ToolError,
    ToolkitNotFoundError,
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
    # Toolkits
    "Toolkit",
    "ToolSpec",
    "ToolResult",
    "rank_tools",
    "validate_arguments",
    "error_code_for_status",
    "ERROR_CODES",
    "ERROR_UNAUTHORIZED",
    "ERROR_FORBIDDEN",
    "ERROR_NOT_FOUND",
    "ERROR_RATE_LIMITED",
    "ERROR_INVALID_ARGUMENTS",
    "ERROR_UNKNOWN_TOOL",
    "ERROR_CONFLICT",
    "ERROR_UPSTREAM_ERROR",
    "ERROR_TIMEOUT",
    # OAuth
    "OAuthSpec",
    "OAuthToken",
    "make_pkce_pair",
    "build_authorize_url",
    "exchange_code",
    "refresh_access_token",
    "revoke_token",
    # Registry
    "discover_connectors",
    "discover_converters",
    "discover_toolkits",
    "get_connector",
    "get_converter",
    "get_toolkit",
    # Errors
    "PipeError",
    "ConnectorNotFoundError",
    "ConfigValidationError",
    "TypeMismatchError",
    "EnvelopeValidationError",
    "PipelineError",
    "ConverterError",
    "ToolkitNotFoundError",
    "ToolError",
    "OAuthError",
]
