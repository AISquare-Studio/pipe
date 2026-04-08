"""Tests for DataEnvelope and supporting types."""

import io

from aisquare.pipe.core.envelope import (
    DataEnvelope,
    MetaField,
    PullParams,
    PushParams,
    PushResult,
    RateLimit,
    Resource,
)


class TestDataEnvelope:
    def test_create_text_envelope(self):
        env = DataEnvelope(
            content_type="text/plain",
            data="Hello",
            source_id="test",
        )
        assert env.content_type == "text/plain"
        assert env.data == "Hello"
        assert env.source_id == "test"
        assert env.metadata == {}
        assert env.schema is None
        assert env.stream is None

    def test_create_bytes_envelope(self):
        env = DataEnvelope(
            content_type="image/png",
            data=b"\x89PNG",
            source_id="test",
        )
        assert isinstance(env.data, bytes)

    def test_create_dict_envelope(self):
        env = DataEnvelope(
            content_type="application/json",
            data={"key": "value"},
            source_id="test",
        )
        assert isinstance(env.data, dict)

    def test_size_bytes(self):
        env = DataEnvelope(
            content_type="image/png",
            data=b"\x89PNG\r\n",
            source_id="test",
        )
        assert env.size() == 6

    def test_size_str(self):
        env = DataEnvelope(
            content_type="text/plain",
            data="Hello",
            source_id="test",
        )
        assert env.size() == 5

    def test_size_str_unicode(self):
        env = DataEnvelope(
            content_type="text/plain",
            data="\u00e9",  # é is 2 bytes in utf-8
            source_id="test",
        )
        assert env.size() == 2

    def test_size_dict_returns_none(self):
        env = DataEnvelope(
            content_type="application/json",
            data={"key": "value"},
            source_id="test",
        )
        assert env.size() is None

    def test_metadata(self):
        env = DataEnvelope(
            content_type="text/plain",
            data="Hello",
            source_id="test",
            metadata={"filename": "test.txt", "author": "bot"},
        )
        assert env.metadata["filename"] == "test.txt"

    def test_stream_field(self):
        buf = io.BytesIO(b"stream data")
        env = DataEnvelope(
            content_type="application/octet-stream",
            data=b"",
            source_id="test",
            stream=buf,
        )
        assert env.stream is not None
        assert env.stream.read() == b"stream data"

    def test_schema_field(self):
        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        env = DataEnvelope(
            content_type="application/json",
            data={"name": "test"},
            source_id="test",
            schema=schema,
        )
        assert env.schema == schema


class TestPullParams:
    def test_get_set(self):
        p = PullParams()
        p["folder_id"] = "abc"
        assert p["folder_id"] == "abc"

    def test_contains(self):
        p = PullParams(params={"key": "val"})
        assert "key" in p
        assert "missing" not in p

    def test_get_default(self):
        p = PullParams()
        assert p.get("missing", "default") == "default"


class TestPushParams:
    def test_get_set(self):
        p = PushParams()
        p["target"] = "folder"
        assert p["target"] == "folder"


class TestPushResult:
    def test_success(self):
        r = PushResult(success=True, ref="item-1")
        assert r.success is True
        assert r.ref == "item-1"
        assert r.error is None

    def test_failure(self):
        r = PushResult(success=False, error="Network timeout")
        assert r.success is False
        assert r.error == "Network timeout"

    def test_default_metadata(self):
        r = PushResult(success=True)
        assert r.metadata == {}


class TestResource:
    def test_create(self):
        r = Resource(id="1", name="file.txt", resource_type="file")
        assert r.id == "1"
        assert r.name == "file.txt"
        assert r.metadata == {}


class TestRateLimit:
    def test_defaults(self):
        rl = RateLimit()
        assert rl.requests_per_second is None
        assert rl.requests_per_minute is None
        assert rl.concurrent is None

    def test_custom(self):
        rl = RateLimit(requests_per_second=10.0, concurrent=5)
        assert rl.requests_per_second == 10.0
        assert rl.concurrent == 5


class TestMetaField:
    def test_create(self):
        mf = MetaField(type=str, required=True, description="A filename")
        assert mf.type is str
        assert mf.required is True
        assert mf.description == "A filename"
        assert mf.max_length is None
        assert mf.default is None
