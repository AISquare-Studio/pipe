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
