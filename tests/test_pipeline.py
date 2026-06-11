"""Tests for the Pipeline class."""

from aisquare.pipe.core.connector import AuthType, SinkConnector
from aisquare.pipe.core.envelope import (
    DataEnvelope,
    MetaField,
    PushResult,
)
from aisquare.pipe.core.merge import MergeStrategy
from aisquare.pipe.core.pipeline import Pipeline
from aisquare.pipe.core.types import MatchLevel
from aisquare.pipe.testing.mock_connectors import MockConverter, MockSink, MockSource


class TestPipelineSingleSource:
    def test_happy_path(self):
        source = MockSource(count=5)
        sink = MockSink()
        pipe = Pipeline(source=source, sink=sink)
        result = pipe.run({})
        assert result.success_count == 5
        assert result.failure_count == 0
        assert len(sink.received) == 5

    def test_envelope_data_flows_through(self):
        envelopes = [
            DataEnvelope(
                content_type="text/plain",
                data="hello",
                source_id="mock-source",
            )
        ]
        source = MockSource(envelopes=envelopes)
        sink = MockSink()
        pipe = Pipeline(source=source, sink=sink)
        pipe.run({})
        assert sink.received[0].data == "hello"

    def test_type_mismatch_fails_gracefully(self):
        """Envelope with incompatible type should be counted as failure, not crash."""
        envelopes = [
            DataEnvelope(
                content_type="video/mp4",
                data=b"video-bytes",
                source_id="mock-source",
            )
        ]
        source = MockSource(envelopes=envelopes)
        sink = MockSink()  # only accepts text/plain, application/json
        pipe = Pipeline(source=source, sink=sink)
        result = pipe.run({})
        assert result.failure_count == 1
        assert result.success_count == 0
        assert len(result.errors) == 1

    def test_with_converter(self):
        """Pipeline should use converter to bridge type mismatch."""
        sink = MockSink()
        sink.input_types = ["application/json"]  # only json

        source = MockSource(count=2)  # produces text/plain
        converter = MockConverter()
        pipe = Pipeline(source=source, sink=sink, converters=[converter])
        result = pipe.run({})
        assert result.success_count == 2
        # Data should be converted to dict
        assert isinstance(sink.received[0].data, dict)
        assert "text" in sink.received[0].data

    def test_individual_failure_doesnt_stop_pipeline(self):
        """One bad envelope shouldn't stop the rest from processing."""

        class FailOnSecondSink(SinkConnector):
            name = "fail-sink"
            version = "0.1.0"
            input_types = ["text/plain"]
            auth_type = AuthType.NONE
            _count = 0

            def push(self, envelope, config, params=None):
                self._count += 1
                if self._count == 2:
                    raise RuntimeError("Simulated failure")
                return PushResult(success=True, ref=f"ok-{self._count}")

            def validate_config(self, config):
                return True

        source = MockSource(count=3)
        sink = FailOnSecondSink()
        pipe = Pipeline(source=source, sink=sink)
        result = pipe.run({})
        assert result.success_count == 2
        assert result.failure_count == 1


class TestPipelineMultiSource:
    def test_concat_merge(self):
        s1 = MockSource(count=2)
        s2 = MockSource(count=3)
        sink = MockSink()
        pipe = Pipeline(source=[s1, s2], sink=sink, merge=MergeStrategy.CONCAT)
        result = pipe.run({})
        assert result.success_count == 5

    def test_batch_merge(self):
        s1 = MockSource(count=2)
        s2 = MockSource(count=2)
        sink = MockSink()
        pipe = Pipeline(source=[s1, s2], sink=sink, merge=MergeStrategy.BATCH)
        result = pipe.run({})
        assert result.success_count == 4

    def test_zip_merge(self):
        s1 = MockSource(count=3)
        s2 = MockSource(count=2)
        sink = MockSink()
        pipe = Pipeline(source=[s1, s2], sink=sink, merge=MergeStrategy.ZIP)
        result = pipe.run({})
        # ZIP stops at shortest (2)
        assert result.success_count == 2

    def test_enrich_merge(self):
        primary_envs = [
            DataEnvelope(
                content_type="text/plain",
                data="main data",
                source_id="primary",
            )
        ]
        secondary_envs = [
            DataEnvelope(
                content_type="application/json",
                data={"extra_key": "extra_value"},
                source_id="secondary",
            )
        ]
        primary = MockSource(envelopes=primary_envs)
        secondary = MockSource(envelopes=secondary_envs)
        sink = MockSink()
        pipe = Pipeline(
            source={"primary": primary, "tags": secondary},
            sink=sink,
            merge=MergeStrategy.ENRICH,
        )
        result = pipe.run({})
        assert result.success_count == 1
        # Secondary dict data should be merged into metadata
        assert sink.received[0].metadata.get("extra_key") == "extra_value"

    def test_multi_source_without_merge_raises(self):
        import pytest
        from aisquare.pipe.errors import PipelineError

        s1 = MockSource(count=1)
        s2 = MockSource(count=1)
        sink = MockSink()
        pipe = Pipeline(source=[s1, s2], sink=sink)
        with pytest.raises(PipelineError, match="merge strategy"):
            pipe.run({})


class TestDryRun:
    def test_compatible(self):
        source = MockSource()
        sink = MockSink()
        pipe = Pipeline(source=source, sink=sink)
        report = pipe.dry_run({})
        assert report.compatible is True
        assert report.match_level in (MatchLevel.EXACT, MatchLevel.WILDCARD)

    def test_incompatible(self):
        source = MockSource()
        source.output_types = ["video/mp4"]
        sink = MockSink()
        pipe = Pipeline(source=source, sink=sink)
        report = pipe.dry_run({})
        assert report.compatible is False
        assert len(report.errors) > 0

    def test_warnings_for_missing_metadata(self):
        source = MockSource()
        sink = MockSink()
        sink.metadata_spec = {
            "required_field": MetaField(type=str, required=True),
        }
        pipe = Pipeline(source=source, sink=sink)
        report = pipe.dry_run({})
        # Source doesn't declare required_field
        assert any("required_field" in w for w in report.warnings)
