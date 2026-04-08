"""Type matching and conversion system for aisquare.pipe."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum

from aisquare.pipe.core.envelope import DataEnvelope

logger = logging.getLogger("aisquare.pipe")


class MatchLevel(Enum):
    """How closely a source output type matches a sink input type."""

    EXACT = 1
    WILDCARD = 2
    CONVERTER = 3
    AGENT = 4
    NONE = 0


class TypeConverter(ABC):
    """Base class for type converters. Transforms one envelope type to another."""

    from_type: str
    to_type: str

    @abstractmethod
    def convert(self, envelope: DataEnvelope) -> DataEnvelope:
        """Convert an envelope from one content type to another."""
        ...


@dataclass
class MatchResult:
    """Result of a type compatibility check."""

    level: MatchLevel
    converter: TypeConverter | None = None
    message: str = ""


def _mime_matches(pattern: str, candidate: str) -> bool:
    """Check if a MIME pattern matches a candidate type.

    Supports exact match, wildcard (image/*), and universal (*/*).
    """
    if pattern == "*/*":
        return True
    if pattern == candidate:
        return True
    if pattern.endswith("/*"):
        prefix = pattern.split("/")[0]
        if candidate.startswith(prefix + "/"):
            return True
    return False


class TypeMatcher:
    """Resolves compatibility between source output and sink input."""

    def __init__(self) -> None:
        self._converters: list[TypeConverter] = []

    def register_converter(self, converter: TypeConverter) -> None:
        self._converters.append(converter)

    def match(self, source_type: str, sink_input_types: list[str]) -> MatchResult:
        """Check compatibility at all levels.

        Returns the best (lowest enum value) match found:
        1. Exact match
        2. Wildcard match
        3. Converter match
        """
        # Check exact match
        for sink_type in sink_input_types:
            if source_type == sink_type:
                return MatchResult(
                    level=MatchLevel.EXACT,
                    message=f"Exact match: {source_type}",
                )

        # Check wildcard match
        for sink_type in sink_input_types:
            if sink_type != source_type and _mime_matches(sink_type, source_type):
                return MatchResult(
                    level=MatchLevel.WILDCARD,
                    message=f"Wildcard match: {source_type} -> {sink_type}",
                )

        # Check converter match
        converter = self.find_converter(source_type, sink_input_types)
        if converter is not None:
            return MatchResult(
                level=MatchLevel.CONVERTER,
                converter=converter,
                message=(
                    f"Converter match: {source_type} -> {converter.to_type} "
                    f"via {type(converter).__name__}"
                ),
            )

        return MatchResult(
            level=MatchLevel.NONE,
            message=f"No match for {source_type} against {sink_input_types}",
        )

    def find_converter(
        self, from_type: str, to_types: list[str]
    ) -> TypeConverter | None:
        """Find a registered converter that can bridge from_type to any of to_types."""
        for converter in self._converters:
            if _mime_matches(converter.from_type, from_type):
                for to_type in to_types:
                    if _mime_matches(to_type, converter.to_type):
                        return converter
        return None
