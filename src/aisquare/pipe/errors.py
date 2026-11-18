"""Custom exceptions for aisquare.pipe."""

import math


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
    """Raised for general pipeline execution errors, and by a source for an
    upstream or transient failure: the provider failed, refused the
    credential, throttled or did not answer in time.

    pipe-as-service answers it 502 ``upstream_error``. It looks the class up
    by its exact type, so a source raises ``PipelineError`` itself, never a
    subclass (a subclass answers 500). What the caller can fix (a bad param,
    a cap, an invalid query, a missing table) is a ``ValueError`` instead,
    answered 422.

    ``retry_after`` (seconds) is set when the provider said when to retry,
    for a host to send as ``Retry-After``. The message is the connector's
    own words, never the provider's error text, SQL, names or values: hosts
    log it at ERROR, with the traceback.
    """

    def __init__(self, *args: object, retry_after: float | None = None) -> None:
        super().__init__(*args)
        self.retry_after: float | None = (
            float(retry_after)
            if isinstance(retry_after, (int, float))
            and not isinstance(retry_after, bool)
            and math.isfinite(retry_after)
            and retry_after >= 0
            else None
        )


class ConverterError(PipeError):
    """Raised when a type converter fails."""


class ToolkitNotFoundError(PipeError):
    """Raised when a requested toolkit is not found in the registry."""


class ToolkitConflictError(PipeError):
    """Raised when installed toolkits cannot be told apart by slug: two share
    a ``slug_prefix``, or one's prefix starts another's. ``installed_toolkits(
    strict=True)`` also raises it for a toolkit left out for its name (one
    hosts can't route, or a class name that differs from its entry point)."""


class ToolError(PipeError):
    """Raised inside a toolkit's ``run_tool`` for an expected tool failure.

    ``Toolkit.call_tool`` turns it into ``ToolResult(successful=False)`` with
    ``str(error)`` as the message (keep it human-readable and free of secrets
    and argument payloads), ``code`` as ``error_code`` — one of the
    ``ERROR_*`` constants in :mod:`aisquare.pipe.core.tools` — and
    ``retry_after`` (seconds) for ``rate_limited``.
    """

    def __init__(
        self,
        message: str = "",
        *,
        code: str = "upstream_error",
        retry_after: float | None = None,
    ) -> None:
        from aisquare.pipe.core.tools import ERROR_CODES  # lazy: tools imports errors

        if code not in ERROR_CODES:
            raise ValueError(
                f"ToolError code {code!r} is not one of: {', '.join(sorted(ERROR_CODES))}"
            )
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


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
