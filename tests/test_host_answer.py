"""PipelineError.retry_after, and aisquare.pipe.testing.host (the status
pipe-as-service answers a source's exception with)."""

from __future__ import annotations

import pickle

import pytest

from aisquare.pipe.errors import (
    ConfigValidationError,
    ConnectorNotFoundError,
    PipeError,
    PipelineError,
    ToolError,
)
from aisquare.pipe.testing.host import (
    HostAnswer,
    host_answer,
    is_caller_error,
    is_upstream,
)


def test_pipeline_error_keeps_its_old_signature():
    e = PipelineError("Multiple sources require a merge strategy")
    assert str(e) == "Multiple sources require a merge strategy"
    assert e.retry_after is None


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        (30, 30.0),
        (1.5, 1.5),
        (0, 0.0),
        (-1, None),
        (float("nan"), None),
        (float("inf"), None),
        (True, None),
        ("30", None),
        (None, None),
    ],
)
def test_pipeline_error_retry_after(given, kept):
    assert PipelineError("x", retry_after=given).retry_after == kept


def test_pipeline_error_pickles_with_its_retry_after():
    e = pickle.loads(pickle.dumps(PipelineError("x", retry_after=7)))
    assert type(e) is PipelineError and str(e) == "x" and e.retry_after == 7.0


class _SubPipeline(PipelineError):
    pass


class _Request(ValueError):
    pass


class _BothValueFirst(ValueError, ConfigValidationError):
    pass


class _BothPipeFirst(ConfigValidationError, ValueError):
    pass


@pytest.mark.parametrize(
    ("exc", "answer"),
    [
        (PipelineError("x", retry_after=3), HostAnswer(502, "upstream_error", 3.0)),
        (PipelineError("x"), HostAnswer(502, "upstream_error")),
        # exact-type lookup: a subclass is a 500
        (_SubPipeline("x"), HostAnswer(500, "pipe_error")),
        (ConfigValidationError("x"), HostAnswer(422, "config_invalid")),
        (ConnectorNotFoundError("x"), HostAnswer(404, "connector_not_found")),
        (ToolError("x"), HostAnswer(500, "pipe_error")),
        (PipeError("x"), HostAnswer(500, "pipe_error")),
        (ValueError("x"), HostAnswer(422, "bad_request")),
        (_Request("x"), HostAnswer(422, "bad_request")),
        # along the MRO, the first handler wins
        (_BothValueFirst("x"), HostAnswer(422, "bad_request")),
        (_BothPipeFirst("x"), HostAnswer(500, "pipe_error")),
        (TypeError("x"), HostAnswer(500, "internal_error")),
        (RuntimeError("x"), HostAnswer(500, "internal_error")),
        (Exception("x"), HostAnswer(500, "internal_error")),
    ],
)
def test_host_answer(exc, answer):
    assert host_answer(exc) == answer


def test_upstream_and_caller_error():
    assert is_upstream(PipelineError("x"))
    assert not is_upstream(_SubPipeline("x"))
    assert is_caller_error(_Request("x")) and is_caller_error(
        ConfigValidationError("x")
    )
    assert not is_caller_error(RuntimeError("x"))
