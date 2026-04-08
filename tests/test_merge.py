"""Tests for merge strategies."""

from aisquare.pipe.core.envelope import DataEnvelope
from aisquare.pipe.core.merge import (
    MergeStrategy,
    apply_merge,
    merge_batch,
    merge_concat,
    merge_enrich,
    merge_zip,
)


def _make_envelopes(prefix: str, count: int) -> list[DataEnvelope]:
    return [
        DataEnvelope(
            content_type="text/plain",
            data=f"{prefix}-{i}",
            source_id=prefix,
            metadata={"index": i},
        )
        for i in range(count)
    ]


class TestConcat:
    def test_basic(self):
        s1 = iter(_make_envelopes("a", 2))
        s2 = iter(_make_envelopes("b", 3))
        results = list(merge_concat([s1, s2]))
        assert len(results) == 5
        assert results[0].data == "a-0"
        assert results[2].data == "b-0"

    def test_dict_sources(self):
        results = list(
            merge_concat(
                {"x": iter(_make_envelopes("x", 1)), "y": iter(_make_envelopes("y", 2))}
            )
        )
        assert len(results) == 3


class TestBatch:
    def test_basic(self):
        s1 = iter(_make_envelopes("a", 2))
        s2 = iter(_make_envelopes("b", 2))
        results = list(merge_batch([s1, s2]))
        assert len(results) == 4


class TestZip:
    def test_equal_length(self):
        s1 = iter(_make_envelopes("a", 2))
        s2 = iter(_make_envelopes("b", 2))
        results = list(merge_zip([s1, s2]))
        assert len(results) == 2
        # Second source data should be in metadata
        assert "source_1" in results[0].metadata

    def test_unequal_stops_at_shortest(self):
        s1 = iter(_make_envelopes("a", 3))
        s2 = iter(_make_envelopes("b", 1))
        results = list(merge_zip([s1, s2]))
        assert len(results) == 1

    def test_dict_sources(self):
        results = list(
            merge_zip(
                {"alpha": iter(_make_envelopes("a", 2)), "beta": iter(_make_envelopes("b", 2))}
            )
        )
        assert len(results) == 2
        assert "beta" in results[0].metadata


class TestEnrich:
    def test_basic(self):
        primary = iter(
            [
                DataEnvelope(
                    content_type="text/plain",
                    data="main",
                    source_id="primary",
                )
            ]
        )
        tags = iter(
            [
                DataEnvelope(
                    content_type="application/json",
                    data={"tag": "important"},
                    source_id="tags",
                )
            ]
        )
        results = list(merge_enrich({"primary": primary, "tags": tags}))
        assert len(results) == 1
        # Dict data should be merged into metadata
        assert results[0].metadata["tag"] == "important"
        assert results[0].data == "main"

    def test_non_dict_secondary(self):
        primary = iter(
            [
                DataEnvelope(
                    content_type="text/plain",
                    data="main",
                    source_id="primary",
                )
            ]
        )
        extra = iter(
            [
                DataEnvelope(
                    content_type="text/plain",
                    data="extra text",
                    source_id="extra",
                )
            ]
        )
        results = list(merge_enrich({"primary": primary, "extra": extra}))
        assert results[0].metadata["extra"] == "extra text"

    def test_multiple_secondary_envelopes(self):
        primary = iter(_make_envelopes("main", 1))
        multi = iter(_make_envelopes("multi", 3))
        results = list(merge_enrich({"primary": primary, "multi": multi}))
        assert isinstance(results[0].metadata["multi"], list)
        assert len(results[0].metadata["multi"]) == 3


class TestApplyMerge:
    def test_concat_via_apply(self):
        sources = [iter(_make_envelopes("a", 2)), iter(_make_envelopes("b", 2))]
        results = list(apply_merge(MergeStrategy.CONCAT, sources))
        assert len(results) == 4

    def test_enrich_requires_dict(self):
        import pytest

        with pytest.raises(ValueError, match="dict"):
            list(apply_merge(MergeStrategy.ENRICH, [iter([])]))

    def test_enrich_requires_primary_key(self):
        import pytest

        with pytest.raises(ValueError, match="primary"):
            list(apply_merge(MergeStrategy.ENRICH, {"other": iter([])}))
