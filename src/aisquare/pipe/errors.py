"""Custom exceptions for aisquare.pipe."""


class PipeError(Exception):
    """Base exception for all aisquare.pipe errors."""


class ConnectorNotFoundError(PipeError):
    """Raised when a requested connector is not found in the registry."""


class ConfigValidationError(PipeError):
    """Raised when connector configuration is invalid."""


class TypeMismatchError(PipeError):
    """Raised when source output type is incompatible with sink input type."""


class EnvelopeValidationError(PipeError):
    """Raised when an envelope fails validation against a sink's metadata spec."""


class PipelineError(PipeError):
    """Raised for general pipeline execution errors."""


class ConverterError(PipeError):
    """Raised when a type converter fails."""


class ToolkitNotFoundError(PipeError):
    """Raised when a requested toolkit is not found in the registry."""


class ToolError(PipeError):
    """Raised inside a toolkit's ``run_tool`` for an expected tool failure.

    ``Toolkit.call_tool`` turns it into ``ToolResult(successful=False)`` with
    ``str(error)`` as the message, so keep it human-readable and free of
    secrets and argument payloads.
    """


class OAuthError(PipeError):
    """Raised when an OAuth flow step fails.

    Carries the provider's error code (``error``), its description, and the
    HTTP status when there was one. Never carries tokens, auth codes, or
    client secrets.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        error: str | None = None,
        description: str | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.error = error
        self.description = description
        self.status = status
