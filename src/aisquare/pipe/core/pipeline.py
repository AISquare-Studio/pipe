"""Pipeline orchestration: source -> [merge] -> [convert] -> sink."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field

from aisquare.pipe.core.connector import SinkConnector, SourceConnector
from aisquare.pipe.core.envelope import DataEnvelope, PushResult
from aisquare.pipe.core.merge import MergeStrategy, apply_merge
from aisquare.pipe.core.types import MatchLevel, MatchResult, TypeConverter, TypeMatcher
from aisquare.pipe.errors import (
    ConfigValidationError,
    PipelineError,
    TypeMismatchError,
)

logger = logging.getLogger("aisquare.pipe")


@dataclass
class PipelineResult:
    """Outcome of a pipeline run."""

    success_count: int = 0
    failure_count: int = 0
    errors: list[dict] = field(default_factory=list)
    results: list[PushResult] = field(default_factory=list)


@dataclass
class CompatibilityReport:
    """Result of a dry-run compatibility check."""

    compatible: bool
    match_level: MatchLevel
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class Pipeline:
    """Orchestrates data transfer from source(s) to a sink."""

    def __init__(
        self,
        source: SourceConnector | list[SourceConnector] | dict[str, SourceConnector],
        sink: SinkConnector,
        converters: list[TypeConverter] | None = None,
        merge: MergeStrategy | None = None,
        agent: bool = False,
    ) -> None:
        self.source = source
        self.sink = sink
        self.merge = merge
        self.agent = agent

        self._matcher = TypeMatcher()
        if converters:
            for c in converters:
                self._matcher.register_converter(c)

    def _get_sources(self) -> list[SourceConnector]:
        """Normalize source(s) into a list."""
        if isinstance(self.source, dict):
            return list(self.source.values())
        if isinstance(self.source, list):
            return self.source
        return [self.source]

    def _validate_configs(self, config: dict) -> None:
        """Validate all connector configs."""
        for src in self._get_sources():
            src_config = config.get(src.name, config)
            if not src.validate_config(src_config):
                raise ConfigValidationError(
                    f"Config validation failed for source '{src.name}'"
                )

        sink_config = config.get(self.sink.name, config)
        if not self.sink.validate_config(sink_config):
            raise ConfigValidationError(
                f"Config validation failed for sink '{self.sink.name}'"
            )

    def _pull_envelopes(self, config: dict) -> Iterator[DataEnvelope]:
        """Pull envelopes, applying merge strategy if multiple sources."""
        sources = self._get_sources()

        if len(sources) == 1:
            src_config = config.get(sources[0].name, config)
            yield from sources[0].pull(src_config)
            return

        if self.merge is None:
            raise PipelineError(
                "Multiple sources require a merge strategy"
            )

        if isinstance(self.source, dict):
            source_iters: dict[str, Iterator[DataEnvelope]] = {}
            for name, src in self.source.items():
                src_config = config.get(src.name, config)
                source_iters[name] = src.pull(src_config)
            yield from apply_merge(self.merge, source_iters)
        else:
            source_list: list[Iterator[DataEnvelope]] = []
            for src in sources:
                src_config = config.get(src.name, config)
                source_list.append(src.pull(src_config))
            yield from apply_merge(self.merge, source_list)

    def _check_metadata(self, envelope: DataEnvelope) -> list[str]:
        """Validate envelope metadata against sink's metadata_spec.

        Returns a list of warnings.
        """
        warnings: list[str] = []
        for key, spec in self.sink.metadata_spec.items():
            if spec.required and key not in envelope.metadata:
                warnings.append(
                    f"Missing required metadata '{key}' for sink '{self.sink.name}'"
                )
        return warnings

    def run(self, config: dict) -> PipelineResult:
        """Execute the pipeline end-to-end."""
        self._validate_configs(config)

        result = PipelineResult()

        for idx, envelope in enumerate(self._pull_envelopes(config)):
            try:
                # Type matching
                match = self._matcher.match(
                    envelope.content_type, self.sink.input_types
                )

                if match.level == MatchLevel.NONE:
                    raise TypeMismatchError(
                        f"No type match for '{envelope.content_type}' "
                        f"against sink '{self.sink.name}' "
                        f"(accepts {self.sink.input_types})"
                    )

                # Apply converter if needed
                if match.level == MatchLevel.CONVERTER and match.converter:
                    logger.info(
                        "Converting %s via %s",
                        envelope.content_type,
                        type(match.converter).__name__,
                    )
                    envelope = match.converter.convert(envelope)

                # Metadata validation (warn only)
                warnings = self._check_metadata(envelope)
                for w in warnings:
                    logger.warning(w)

                # Acceptance check
                if not self.sink.accepts(envelope):
                    raise TypeMismatchError(
                        f"Sink '{self.sink.name}' rejected envelope "
                        f"with content_type '{envelope.content_type}'"
                    )

                # Size check
                max_sz = self.sink.max_size()
                env_sz = envelope.size()
                if max_sz is not None and env_sz is not None and env_sz > max_sz:
                    raise PipelineError(
                        f"Envelope size {env_sz} exceeds sink max size {max_sz}"
                    )

                # Push
                sink_config = config.get(self.sink.name, config)
                push_result = self.sink.push(envelope, sink_config)
                result.results.append(push_result)

                if push_result.success:
                    result.success_count += 1
                else:
                    result.failure_count += 1
                    result.errors.append(
                        {
                            "envelope_index": idx,
                            "error": push_result.error or "Push returned failure",
                            "source_id": envelope.source_id,
                        }
                    )

            except Exception as e:
                logger.error("Envelope %d failed: %s", idx, e)
                result.failure_count += 1
                result.errors.append(
                    {
                        "envelope_index": idx,
                        "error": str(e),
                        "source_id": envelope.source_id,
                    }
                )

        return result

    def dry_run(self, config: dict) -> CompatibilityReport:
        """Check compatibility without moving data."""
        warnings: list[str] = []
        errors: list[str] = []

        # Validate configs
        for src in self._get_sources():
            src_config = config.get(src.name, config)
            try:
                if not src.validate_config(src_config):
                    errors.append(f"Config validation failed for source '{src.name}'")
            except Exception as e:
                errors.append(f"Source '{src.name}' config error: {e}")

        sink_config = config.get(self.sink.name, config)
        try:
            if not self.sink.validate_config(sink_config):
                errors.append(f"Config validation failed for sink '{self.sink.name}'")
        except Exception as e:
            errors.append(f"Sink '{self.sink.name}' config error: {e}")

        # Check type compatibility for all sources
        best_match = MatchLevel.NONE
        for src in self._get_sources():
            for out_type in src.output_types:
                match = self._matcher.match(out_type, self.sink.input_types)
                if match.level != MatchLevel.NONE:
                    if best_match == MatchLevel.NONE or match.level.value < best_match.value:
                        best_match = match.level
                else:
                    warnings.append(
                        f"No match for source type '{out_type}' "
                        f"from '{src.name}'"
                    )

        if best_match == MatchLevel.NONE:
            errors.append(
                f"No type compatibility between source(s) and sink '{self.sink.name}'"
            )

        # Multi-source checks
        sources = self._get_sources()
        if len(sources) > 1 and self.merge is None:
            errors.append("Multiple sources require a merge strategy")

        # Metadata spec warnings
        for src in sources:
            for key, spec in self.sink.metadata_spec.items():
                if spec.required and key not in src.metadata_spec:
                    warnings.append(
                        f"Sink requires metadata '{key}' but source "
                        f"'{src.name}' does not declare it"
                    )

        return CompatibilityReport(
            compatible=len(errors) == 0,
            match_level=best_match,
            warnings=warnings,
            errors=errors,
        )
