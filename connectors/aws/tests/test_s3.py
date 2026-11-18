"""s3-source and the s3 toolkit, on moto (STS + S3).

moto partitions resources by account: the test bucket is created through the
assumed role, so it lives in the customer account, where the code must reach it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aisquare.pipe import PullParams
from aisquare.pipe.errors import ConfigValidationError

from aisquare_pipe_aws import s3
from aisquare_pipe_aws.connector import S3Source
from aisquare_pipe_aws.credentials import (
    AwsNotConfiguredError,
    CrossAccountConfig,
    assume_role_session,
)
from aisquare_pipe_aws.toolkit import S3Toolkit
from tests.conftest import (
    AMBIENT_KEY,
    CUSTOMER_ACCOUNT,
    EXTERNAL_ID,
    access_key_of,
    blob,
)

BUCKET = "policy-docs"


@pytest.fixture
def bucket(aws):
    """The customer's bucket: 8 documents under rulebooks/, 2 outside it, one folder marker."""
    client = assume_role_session(CrossAccountConfig.from_config(blob())).client(
        "s3", region_name="us-east-1"
    )
    client.create_bucket(Bucket=BUCKET)
    for n in range(8):
        client.put_object(
            Bucket=BUCKET,
            Key=f"rulebooks/policy-{n}.md",
            Body=f"# Rule book {n}\n".encode(),
            ContentType="text/markdown",
        )
    client.put_object(Bucket=BUCKET, Key="rulebooks/sub/", Body=b"")
    client.put_object(Bucket=BUCKET, Key="other/secret.txt", Body=b"not yours")
    client.put_object(Bucket=BUCKET, Key="other/x.bin", Body=b"\x00\x01")
    return client


@pytest.fixture
def customer_clients(monkeypatch):
    """Every S3 client the code builds, and how many ListObjectsV2 calls each made."""
    built: list[dict[str, Any]] = []
    real = s3.client

    def build(session: Any, location: Any) -> Any:
        client = real(session, location)
        entry = {"client": client, "lists": 0}

        def count(**_: Any) -> None:
            entry["lists"] += 1

        client.meta.events.register("before-call.s3.ListObjectsV2", count)
        built.append(entry)
        return client

    monkeypatch.setattr(s3, "client", build)
    return built


def _pull(config: dict, **params: Any) -> list:
    return list(S3Source().pull(config, PullParams(params) if params else None))


def test_every_object_under_the_prefix_on_every_page(
    bucket, customer_clients, assume_calls, monkeypatch
):
    monkeypatch.setattr(s3, "PAGE_SIZE", 3)
    envelopes = _pull(blob())
    keys = sorted(e.metadata["key"] for e in envelopes)
    assert keys == [
        f"rulebooks/policy-{n}.md" for n in range(8)
    ]  # no folder marker, nothing outside
    assert (
        envelopes[0].data == b"# Rule book 0\n"
        and envelopes[0].content_type == "text/markdown"
    )
    assert envelopes[0].source_id == f"s3://{BUCKET}/rulebooks/policy-0.md"
    (entry,) = customer_clients
    assert entry["lists"] >= 3  # 9 keys under the prefix at 3 a page
    # Signed as the assumed customer role, never as the service's own key.
    key = access_key_of(entry["client"])
    assert key != AMBIENT_KEY and key.startswith("ASIA")
    assert len(assume_calls) == 1 and assume_calls[0]["ExternalId"] == EXTERNAL_ID


def test_the_version_marker_is_the_etag_or_the_version_id(bucket, assume_calls):
    (first, *_rest) = _pull(blob(), max_objects=1)
    meta = first.metadata
    assert meta["etag"] and not meta["etag"].startswith('"')
    assert meta["version"] == meta["etag"]  # unversioned bucket
    assert meta["last_modified"] and meta["account_id"] == CUSTOMER_ACCOUNT

    bucket.put_bucket_versioning(
        Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"}
    )
    bucket.put_object(
        Bucket=BUCKET, Key="rulebooks/policy-0.md", Body=b"# Rule book 0, edited\n"
    )
    (edited,) = _pull(blob(), max_objects=1)
    assert edited.data == b"# Rule book 0, edited\n"
    assert (
        edited.metadata["version_id"]
        and edited.metadata["version"] == edited.metadata["version_id"]
    )
    assert edited.metadata["etag"] != meta["etag"]


def test_a_null_version_id_is_no_version_id_so_the_marker_is_the_etag(
    bucket, assume_calls, monkeypatch
):
    # In a bucket whose versioning is suspended, S3 stores each write as the
    # "null" version and returns VersionId "null" every time (moto returns no
    # VersionId there), so the marker must be the ETag, which does change.
    bucket.put_bucket_versioning(
        Bucket=BUCKET, VersioningConfiguration={"Status": "Suspended"}
    )
    real = s3.client

    def as_s3_does(session: Any, location: Any) -> Any:
        client = real(session, location)

        def null_version(parsed: dict, **_: Any) -> None:
            parsed["VersionId"] = "null"

        for op in ("GetObject", "HeadObject"):
            client.meta.events.register(f"after-call.s3.{op}", null_version)
        return client

    monkeypatch.setattr(s3, "client", as_s3_does)
    key = "rulebooks/policy-0.md"
    kit = S3Toolkit()

    def read() -> tuple[dict, dict, dict]:
        (envelope,) = _pull(blob(), max_objects=1)
        head = kit.call_tool("S3_HEAD_OBJECT", {"key": key}, blob())
        got = kit.call_tool("S3_GET_OBJECT", {"key": key}, blob())
        assert head.successful and got.successful, (head.error, got.error)
        return envelope.metadata, head.data, got.data

    before = read()
    bucket.put_object(Bucket=BUCKET, Key=key, Body=b"# Rule book 0, edited\n")
    after = read()
    for old, new in zip(before, after, strict=True):
        assert new.get("version_id") is None  # "null" is no version id
        assert old["version"] == old["etag"] and new["version"] == new["etag"]
        assert new["version"] != old["version"]  # the edit shows


@pytest.mark.parametrize(
    ("version_id", "expected"),
    [
        (None, None),
        ("", None),
        ("null", None),
        ("3HL4kqtJlcpXroDTDmJ", "3HL4kqtJlcpXroDTDmJ"),
    ],
)
def test_real_version_id(version_id, expected):
    assert s3.real_version_id(version_id) == expected
    assert s3.version_marker(expected, "etag1") == (expected or "etag1")


def test_modified_after_filters_and_max_object_bytes_skips(bucket, assume_calls):
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    assert _pull(blob(), modified_after=future) == []
    assert len(_pull(blob(), modified_after=past)) == 8
    assert _pull(blob(), max_object_bytes=5) == []  # every document is larger


def test_a_caller_may_narrow_the_prefix_never_leave_it(bucket, assume_calls):
    bucket.put_object(Bucket=BUCKET, Key="rulebooks/eu/one.md", Body=b"eu")
    assert [e.metadata["key"] for e in _pull(blob(), prefix="rulebooks/eu/")] == [
        "rulebooks/eu/one.md"
    ]
    calls_before = len(assume_calls)
    with pytest.raises(ConfigValidationError, match="inside the configured prefix"):
        _pull(blob(), prefix="other/")
    assert len(assume_calls) == calls_before


@pytest.mark.parametrize(
    "params",
    [
        {"bucket": "someone-elses"},
        {"region": "eu-west-1"},
        {"role_arn": "arn:aws:iam::999999999999:role/x"},
        {"endpoint_url": "http://169.254.169.254"},
        {"aws_secret_access_key": "x"},
    ],
)
def test_a_caller_cannot_override_a_server_key(params, aws, assume_calls):
    with pytest.raises(ConfigValidationError, match="unsupported pull param"):
        _pull(blob(), **params)
    assert assume_calls == []


def test_no_s3_section_or_no_blob_fails_closed():
    with pytest.raises(AwsNotConfiguredError, match="no s3.bucket"):
        _pull(blob(s3=None))
    with pytest.raises(AwsNotConfiguredError):
        next(S3Source().pull({}))
    assert S3Source().validate_config(blob()) is True
    assert S3Source().validate_config({}) is False


# -- the toolkit -------------------------------------------------------------------


def test_toolkit_lists_pages_with_a_continuation_token(bucket, assume_calls):
    kit = S3Toolkit()
    first = kit.call_tool("S3_LIST_OBJECTS", {"limit": 5}, blob())
    assert first.successful, first.error
    assert len(first.data["items"]) == 5 and first.data["has_more"] is True
    rest = kit.call_tool(
        "S3_LIST_OBJECTS",
        {"limit": 5, "continuation_token": first.data["next_continuation_token"]},
        blob(),
    )
    keys = [i["key"] for i in first.data["items"] + rest.data["items"]]
    assert (
        rest.data["has_more"] is False and len(keys) == 9
    )  # the folder marker is listed here
    assert all(k.startswith("rulebooks/") for k in keys)
    assert len(assume_calls) == 1  # the session is reused across tool calls


def test_toolkit_reads_text_inline_and_binary_as_a_file_and_heads(bucket, assume_calls):
    bucket.put_object(Bucket=BUCKET, Key="rulebooks/scan.pdf", Body=b"%PDF-1.7\x00\xff")
    kit = S3Toolkit()
    text = kit.call_tool("S3_GET_OBJECT", {"key": "rulebooks/policy-3.md"}, blob())
    assert text.successful and text.data["text"] == "# Rule book 3\n" and not text.files
    pdf = kit.call_tool("S3_GET_OBJECT", {"key": "rulebooks/scan.pdf"}, blob())
    assert pdf.successful and pdf.data["file"] is True
    assert (
        pdf.files[0].data == b"%PDF-1.7\x00\xff"
        and pdf.files[0].content_type == "application/pdf"
    )
    head = kit.call_tool("S3_HEAD_OBJECT", {"key": "rulebooks/policy-3.md"}, blob())
    assert (
        head.successful
        and head.data["etag"] == text.data["etag"]
        and head.data["size"] == 14
    )


def test_toolkit_missing_key_is_not_found(bucket, assume_calls):
    result = S3Toolkit().call_tool(
        "S3_GET_OBJECT", {"key": "rulebooks/nope.md"}, blob()
    )
    assert not result.successful and result.error_code == "not_found", result.error


@pytest.mark.parametrize(
    ("slug", "arguments"),
    [
        ("S3_GET_OBJECT", {"key": "other/secret.txt"}),
        ("S3_HEAD_OBJECT", {"key": "other/secret.txt"}),
        ("S3_LIST_OBJECTS", {"prefix": "other/"}),
        ("S3_LIST_OBJECTS", {"bucket": "someone-elses"}),
        ("S3_LIST_OBJECTS", {"region": "eu-west-1"}),
        ("S3_GET_OBJECT", {"key": "rulebooks/a.md", "bucket": "x"}),
        ("S3_LIST_OBJECTS", {"limit": 5000}),
    ],
)
def test_toolkit_refuses_keys_outside_the_prefix_and_server_keys(
    slug, arguments, aws, assume_calls
):
    result = S3Toolkit().call_tool(slug, arguments, blob())
    assert not result.successful and result.error_code == "invalid_arguments", (
        result.error
    )


def test_toolkit_without_s3_is_not_connected():
    kit = S3Toolkit()
    assert kit.validate_config(blob(s3=None)) is False
    result = kit.call_tool("S3_LIST_OBJECTS", {}, blob(s3=None))
    assert not result.successful and result.error_code == "not_connected"
    assert kit.call_tool("S3_LIST_OBJECTS", {}, {}).error_code == "not_connected"
