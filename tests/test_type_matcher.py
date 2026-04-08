"""Tests for the type matching system."""

from aisquare.pipe.core.envelope import DataEnvelope
from aisquare.pipe.core.types import MatchLevel, TypeConverter, TypeMatcher


class StubConverter(TypeConverter):
    from_type = "text/plain"
    to_type = "application/json"

    def convert(self, envelope: DataEnvelope) -> DataEnvelope:
        return DataEnvelope(
            content_type="application/json",
            data={"text": envelope.data},
            source_id=envelope.source_id,
            metadata=envelope.metadata.copy(),
        )


class ImageToPdfConverter(TypeConverter):
    from_type = "image/*"
    to_type = "application/pdf"

    def convert(self, envelope: DataEnvelope) -> DataEnvelope:
        return DataEnvelope(
            content_type="application/pdf",
            data=b"%PDF-fake",
            source_id=envelope.source_id,
            metadata=envelope.metadata.copy(),
        )


class TestTypeMatcher:
    def test_exact_match(self):
        m = TypeMatcher()
        result = m.match("image/png", ["image/png", "image/jpeg"])
        assert result.level == MatchLevel.EXACT

    def test_wildcard_match(self):
        m = TypeMatcher()
        result = m.match("image/png", ["image/*"])
        assert result.level == MatchLevel.WILDCARD

    def test_universal_wildcard(self):
        m = TypeMatcher()
        result = m.match("image/png", ["*/*"])
        assert result.level == MatchLevel.WILDCARD

    def test_no_match(self):
        m = TypeMatcher()
        result = m.match("image/png", ["text/plain", "application/json"])
        assert result.level == MatchLevel.NONE

    def test_converter_match(self):
        m = TypeMatcher()
        m.register_converter(StubConverter())
        result = m.match("text/plain", ["application/json"])
        assert result.level == MatchLevel.CONVERTER
        assert result.converter is not None

    def test_exact_preferred_over_wildcard(self):
        m = TypeMatcher()
        result = m.match("image/png", ["image/*", "image/png"])
        assert result.level == MatchLevel.EXACT

    def test_exact_preferred_over_converter(self):
        m = TypeMatcher()
        m.register_converter(StubConverter())
        result = m.match("text/plain", ["text/plain", "application/json"])
        assert result.level == MatchLevel.EXACT

    def test_wildcard_preferred_over_converter(self):
        m = TypeMatcher()
        m.register_converter(StubConverter())
        result = m.match("text/plain", ["text/*"])
        assert result.level == MatchLevel.WILDCARD

    def test_converter_with_wildcard_from_type(self):
        m = TypeMatcher()
        m.register_converter(ImageToPdfConverter())
        result = m.match("image/png", ["application/pdf"])
        assert result.level == MatchLevel.CONVERTER
        assert result.converter is not None

    def test_find_converter(self):
        m = TypeMatcher()
        m.register_converter(StubConverter())
        c = m.find_converter("text/plain", ["application/json"])
        assert c is not None
        assert c.to_type == "application/json"

    def test_find_converter_no_match(self):
        m = TypeMatcher()
        c = m.find_converter("text/plain", ["application/json"])
        assert c is None

    def test_no_match_message(self):
        m = TypeMatcher()
        result = m.match("video/mp4", ["text/plain"])
        assert result.level == MatchLevel.NONE
        assert "No match" in result.message

    def test_multiple_converters(self):
        m = TypeMatcher()
        m.register_converter(StubConverter())
        m.register_converter(ImageToPdfConverter())
        # text/plain -> application/json
        r1 = m.match("text/plain", ["application/json"])
        assert r1.level == MatchLevel.CONVERTER
        # image/jpeg -> application/pdf
        r2 = m.match("image/jpeg", ["application/pdf"])
        assert r2.level == MatchLevel.CONVERTER
