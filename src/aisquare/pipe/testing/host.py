"""How pipe-as-service answers an exception a connector raises, for tests.

A source's ``pull`` runs inside the host's request handler with nothing
catching around it, so the exception's TYPE decides the HTTP answer. This
mirrors pipe-as-service ``api/errors.py`` (release/pipe-router,
feat/native-auto-routing @ d6fcca9):

* handlers are found along the exception's MRO, as Starlette does: the first
  of ``PipeError`` or ``ValueError`` it meets wins;
* a ``PipeError`` is looked up by its EXACT type: ``PipelineError`` is 502
  ``upstream_error``, ``ConfigValidationError`` 422 ``config_invalid`` (and
  the others below); any other ``PipeError``, a subclass included, is 500
  ``pipe_error``;
* a ``ValueError`` (any subclass) is 422 ``bad_request``;
* anything else is 500 (the traceback, with the exception's text, goes to
  the host's log).

So a source raises ``PipelineError`` itself for an upstream or transient
failure and a ``ValueError`` subclass for what the caller can fix.
:func:`host_answer` lets a connector's tests pin that without the host.

This mirror is deliberately the STRICTEST host, not the newest one:
pipe-as-service fix/source-errors-host (84de644, not yet on its router)
looks ``PipeError`` up along the MRO, so there a ``PipelineError`` subclass
answers 502 too. A source that passes here passes on both; one that relies
on a subclass would answer 500 on any host still matching exact types.
``retry_after`` is the exception's own ``retry_after`` (what a host sends as
``Retry-After``), when it has one.
"""

from __future__ import annotations

from dataclasses import dataclass

from aisquare.pipe.errors import (
    ConfigValidationError,
    ConnectorNotFoundError,
    ConverterError,
    EnvelopeValidationError,
    PipeError,
    PipelineError,
    TypeMismatchError,
)

#: pipe-as-service ``_PIPE_STATUS``: exact type -> (status, error_code).
PIPE_STATUS: dict[type[PipeError], tuple[int, str]] = {
    ConnectorNotFoundError: (404, "connector_not_found"),
    ConfigValidationError: (422, "config_invalid"),
    TypeMismatchError: (409, "type_mismatch"),
    EnvelopeValidationError: (422, "envelope_invalid"),
    ConverterError: (422, "converter_error"),
    PipelineError: (502, "upstream_error"),
}


@dataclass(frozen=True)
class HostAnswer:
    status: int
    error_code: str
    retry_after: float | None = None


def host_answer(exc: BaseException) -> HostAnswer:
    """The HTTP status and error code pipe-as-service answers ``exc`` with."""
    retry_after = getattr(exc, "retry_after", None)
    if not isinstance(retry_after, (int, float)) or isinstance(retry_after, bool):
        retry_after = None
    for cls in type(exc).__mro__:
        if cls is PipeError:
            status, code = PIPE_STATUS.get(type(exc), (500, "pipe_error"))  # type: ignore[arg-type]
            return HostAnswer(status, code, retry_after)
        if cls is ValueError:
            return HostAnswer(422, "bad_request", retry_after)
    return HostAnswer(500, "internal_error", retry_after)


def is_upstream(exc: BaseException) -> bool:
    """True when the host answers ``exc`` 502 ``upstream_error``."""
    return host_answer(exc).status == 502


def is_caller_error(exc: BaseException) -> bool:
    """True when the host answers ``exc`` 422 (the caller can fix it)."""
    return host_answer(exc).status == 422


__all__ = [
    "PIPE_STATUS",
    "HostAnswer",
    "host_answer",
    "is_caller_error",
    "is_upstream",
]
