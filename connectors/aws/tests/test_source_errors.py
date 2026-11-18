"""How s3-source and bedrock-agentcore-policies-source fail, as
pipe-as-service answers it: 502 upstream_error (a PipelineError) for what
AWS refused or failed, 422 (a ValueError) for what the caller can fix.
Never a bare PipeError or Exception (500), and never AWS's message text (the
#37 review's m2). Real botocore clients with a Stubber: no network."""

from __future__ import annotations

import io
import ssl
from typing import Any

import boto3
import pytest
from aisquare.pipe import PullParams
from aisquare.pipe.errors import ConfigValidationError, PipelineError
from aisquare.pipe.testing.host import host_answer
from botocore.exceptions import (
    ConnectionClosedError,
    EndpointConnectionError,
    ReadTimeoutError,
    ResponseStreamingError,
)
from botocore.response import StreamingBody
from botocore.stub import Stubber
from urllib3.response import HTTPResponse

from aisquare_pipe_aws import agentcore, connector, s3
from aisquare_pipe_aws.connector import (
    AwsRequestError,
    BedrockAgentCorePoliciesSource,
    S3Source,
)
from aisquare_pipe_aws.credentials import AwsAccessError, AwsNotConfiguredError
from tests.conftest import blob

#: In every faked AWS error message: it must never reach the source's error.
LEAK = "secret-payroll-xyz"
BUCKET = "policy-docs"
S3_BLOB = blob(s3={"bucket": BUCKET, "prefix": "rulebooks/", "region": "us-east-1"})


def _session(self: Any, cfg: Any) -> Any:
    return boto3.Session(
        aws_access_key_id="AKIAFAKEFAKEFAKEFAKE",
        aws_secret_access_key="fake",
        region_name="us-east-1",
    )


@pytest.fixture
def stubbed(monkeypatch):
    """Queue (method, response | (code, status)) per service before a pull;
    every client the source builds is stubbed with them."""
    queued: dict[str, list[tuple[str, Any]]] = {"s3": [], "agentcore": []}
    stubs: list[Stubber] = []
    monkeypatch.setattr(connector._AwsSource, "_session", _session)

    def stub(client: Any, kind: str) -> Any:
        stubber = Stubber(client)
        for method, answer in queued[kind]:
            if isinstance(answer, tuple):
                code, status = answer
                stubber.add_client_error(
                    method,
                    service_error_code=code,
                    service_message=f"{code}: {LEAK}",
                    http_status_code=status,
                )
            else:
                stubber.add_response(method, answer)
        stubber.activate()
        stubs.append(stubber)
        return client

    monkeypatch.setattr(
        s3,
        "client",
        lambda session, location: stub(
            session.client("s3", region_name=location.region), "s3"
        ),
    )
    monkeypatch.setattr(
        agentcore,
        "client",
        lambda session, region: stub(
            session.client(agentcore.SERVICE, region_name=region), "agentcore"
        ),
    )
    return queued


def _s3_pull(**params: Any) -> list:
    return list(S3Source().pull(S3_BLOB, PullParams(params) if params else None))


def _policies_pull(**params: Any) -> list:
    return list(
        BedrockAgentCorePoliciesSource().pull(
            blob(), PullParams(params) if params else None
        )
    )


UPSTREAM = [
    ("AccessDenied", 403, "AccessDenied"),
    ("SlowDown", 503, "SlowDown"),
    ("ExpiredToken", 400, "ExpiredToken"),
    ("InternalError", 500, "InternalError"),
]
CALLER = [
    ("NoSuchBucket", 404, "not_found"),
    ("InvalidArgument", 400, "invalid_arguments"),
]


@pytest.mark.parametrize(("code", "status", "words"), UPSTREAM)
def test_s3_refusals_and_failures_are_a_502(stubbed, code, status, words):
    stubbed["s3"].append(("list_objects_v2", (code, status)))
    with pytest.raises(PipelineError) as raised:
        _s3_pull()
    e = raised.value
    assert type(e) is PipelineError  # the host maps the exact type
    assert host_answer(e).status == 502
    assert "s3:ListObjectsV2" in str(e) and words in str(e)
    assert LEAK not in str(e)
    assert e.__cause__ is None and e.__suppress_context__


@pytest.mark.parametrize(("code", "status", "kind"), CALLER)
def test_s3_what_the_caller_can_fix_is_a_422(stubbed, code, status, kind):
    stubbed["s3"].append(("list_objects_v2", (code, status)))
    with pytest.raises(AwsRequestError) as raised:
        _s3_pull()
    e = raised.value
    assert isinstance(e, ValueError) and (e.code, e.aws_code) == (kind, code)
    assert host_answer(e).status == 422
    assert LEAK not in str(e)


def test_s3_get_object_refused_is_a_502(stubbed):
    listed = {
        "Contents": [{"Key": "rulebooks/a.md", "Size": 3, "ETag": '"e"'}],
        "IsTruncated": False,
    }
    stubbed["s3"] += [
        ("list_objects_v2", listed),
        ("get_object", ("AccessDenied", 403)),
    ]
    with pytest.raises(PipelineError) as raised:
        _s3_pull()
    assert host_answer(raised.value).status == 502
    assert "s3:GetObject" in str(raised.value)


def test_an_object_that_grew_past_the_limit_is_skipped_not_raised(stubbed):
    listed = {
        "Contents": [
            {"Key": "rulebooks/a.md", "Size": 3, "ETag": '"a"'},
            {"Key": "rulebooks/b.md", "Size": 3, "ETag": '"b"'},
        ],
        "IsTruncated": False,
    }
    grown = {
        "Body": StreamingBody(io.BytesIO(b"x" * 20), 20),
        "ContentLength": 20,
        "ETag": '"a2"',
    }
    small = {
        "Body": StreamingBody(io.BytesIO(b"abc"), 3),
        "ContentLength": 3,
        "ETag": '"b"',
    }
    stubbed["s3"] += [
        ("list_objects_v2", listed),
        ("get_object", grown),
        ("get_object", small),
    ]
    envelopes = _s3_pull(max_object_bytes=10)
    assert [e.metadata["key"] for e in envelopes] == ["rulebooks/b.md"]


def test_a_connection_failure_is_a_502(monkeypatch):
    monkeypatch.setattr(connector._AwsSource, "_session", _session)
    real = s3.client

    def build(session: Any, location: Any) -> Any:
        client = real(session, location)

        def fail(**_: Any) -> None:
            raise EndpointConnectionError(endpoint_url=f"https://{LEAK}.example")

        client.meta.events.register("before-call.s3.ListObjectsV2", fail)
        return client

    monkeypatch.setattr(s3, "client", build)
    with pytest.raises(PipelineError) as raised:
        _s3_pull()
    assert host_answer(raised.value).status == 502
    assert "EndpointConnectionError" in str(raised.value)
    assert LEAK not in str(raised.value)


@pytest.mark.parametrize(
    ("code", "status", "answer"),
    [
        ("AccessDeniedException", 403, 502),
        ("ThrottlingException", 429, 502),
        ("InternalServerException", 500, 502),
        ("ResourceNotFoundException", 404, 422),
        ("ValidationException", 400, 422),
    ],
)
def test_agentcore_failures(stubbed, code, status, answer):
    stubbed["agentcore"].append(("list_policy_engines", (code, status)))
    with pytest.raises((PipelineError, AwsRequestError)) as raised:
        _policies_pull()
    e = raised.value
    assert host_answer(e).status == answer
    assert type(e) is (PipelineError if answer == 502 else AwsRequestError)
    assert "bedrock-agentcore:ListPolicyEngines" in str(e)
    assert LEAK not in str(e)


def test_a_refused_assume_role_is_a_502(monkeypatch):
    def refused(cfg: Any, *, base_session: Any = None) -> Any:
        raise AwsAccessError(
            "AWS refused sts:AssumeRole: the customer role does not trust this "
            "service's principal",
            code="forbidden",
            aws_code="AccessDenied",
        )

    monkeypatch.setattr(connector, "assume_role_session", refused)
    for pull in (_s3_pull, _policies_pull):
        with pytest.raises(PipelineError) as raised:
            pull()
        assert host_answer(raised.value).status == 502
        assert "sts:AssumeRole" in str(raised.value)


def test_no_principal_on_this_host_is_a_422(monkeypatch):
    def none(cfg: Any, *, base_session: Any = None) -> Any:
        raise AwsNotConfiguredError(
            "AWS is not configured on this host: no service principal"
        )

    monkeypatch.setattr(connector, "assume_role_session", none)
    with pytest.raises(AwsNotConfiguredError) as raised:
        _s3_pull()
    assert host_answer(raised.value).status == 422
    # Still a ConfigValidationError for callers that catch that.
    assert isinstance(raised.value, ConfigValidationError)


@pytest.mark.parametrize(
    ("config", "params"),
    [
        ({}, {}),  # no blob
        (blob(role_arn="nope"), {}),  # a blob that doesn't parse
        (blob(s3=None), {}),  # no s3 section
        (S3_BLOB, {"prefix": "elsewhere/"}),
        (S3_BLOB, {"max_objects": 0}),
        (S3_BLOB, {"bucket": "other"}),
        (S3_BLOB, {"modified_after": "yesterday"}),
    ],
)
def test_s3_refused_blobs_and_params_are_a_422(stubbed, config, params):
    with pytest.raises(ValueError if not params else ConfigValidationError) as raised:
        list(S3Source().pull(config, PullParams(params) if params else None))
    assert host_answer(raised.value).status == 422


@pytest.mark.parametrize(
    "params",
    [
        {"policy_engine_id": "../x"},
        {"max_policies": 0},
        {"updated_after": "x"},
        {"nope": 1},
    ],
)
def test_agentcore_refused_params_are_a_422(stubbed, params):
    with pytest.raises(ConfigValidationError) as raised:
        _policies_pull(**params)
    assert host_answer(raised.value).status == 422


class _BrokenRaw:
    """A GetObject body whose read fails as botocore's stream does."""

    def __init__(self, error: BaseException | None) -> None:
        self.error = error

    def read(self, *_: Any) -> bytes:
        if self.error is not None:
            raise self.error
        return b""  # nothing, though ContentLength said 10: IncompleteReadError

    def close(self) -> None:
        pass


class _BadRecordSocket(io.RawIOBase):
    """A socket file whose next read is a TLS record that fails its MAC."""

    def readable(self) -> bool:
        return True

    def readinto(self, _: Any) -> int:
        raise ssl.SSLError(1, _BAD_MAC)


_BAD_MAC = (
    "[SSL: DECRYPTION_FAILED_OR_BAD_RECORD_MAC] decryption failed or bad record mac"
)


def _tls_failing_body() -> HTTPResponse:
    """What botocore streams from: urllib3's response, not yet read. urllib3
    turns the ``ssl.SSLError`` into its own ``SSLError``, which botocore's
    ``StreamingBody`` passes through (#46 reopen seq 13987, FAIL 1)."""
    return HTTPResponse(body=_BadRecordSocket(), preload_content=False)


_KEY_URL = f"https://{BUCKET}.s3.amazonaws.com/rulebooks/{LEAK}.pdf"


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(ReadTimeoutError(endpoint_url=_KEY_URL), id="ReadTimeoutError"),
        pytest.param(
            ConnectionClosedError(endpoint_url=_KEY_URL), id="ConnectionClosedError"
        ),
        pytest.param(
            ResponseStreamingError(error=OSError(_KEY_URL)), id="ResponseStreamingError"
        ),
        pytest.param(None, id="IncompleteReadError"),
        pytest.param(_tls_failing_body, id="urllib3-SSLError"),
        pytest.param(ConnectionResetError(_KEY_URL), id="OSError"),
    ],
)
def test_a_body_read_that_fails_is_a_502_without_the_key(stubbed, error):
    """GetObject answered, then reading its body failed (#46 reopen seq 13961,
    FAIL 1): botocore's error names the bucket and key, and it was a 500. The
    same for urllib3's own errors and a bare ``OSError``, which botocore does
    not wrap (#46 reopen seq 13987, FAIL 1)."""
    listed = {
        "Contents": [{"Key": f"rulebooks/{LEAK}.pdf", "Size": 10, "ETag": '"e"'}],
        "IsTruncated": False,
    }
    raw = error() if callable(error) else _BrokenRaw(error)
    body = StreamingBody(raw, 10)
    stubbed["s3"] += [
        ("list_objects_v2", listed),
        ("get_object", {"Body": body, "ContentLength": 10, "ETag": '"e"'}),
    ]
    with pytest.raises(PipelineError) as raised:
        _s3_pull()
    e = raised.value
    assert type(e) is PipelineError
    assert host_answer(e).status == 502
    assert "s3:GetObject" in str(e)
    assert LEAK not in str(e)
    assert "DECRYPTION_FAILED" not in str(e)
    assert e.__cause__ is None and e.__suppress_context__
