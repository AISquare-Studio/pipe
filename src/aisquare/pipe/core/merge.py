"""Merge strategies for multi-source pipelines."""

from __future__ import annotations

import copy
import logging
from collections.abc import Iterator
from enum import Enum

from aisquare.pipe.core.envelope import DataEnvelope

logger = logging.getLogger("aisquare.pipe")


class MergeStrategy(Enum):
    """How to combine envelopes from multiple sources."""

    ENRICH = "enrich"
    BATCH = "batch"
    ZIP = "zip"
    CONCAT = "concat"


def merge_enrich(
    sources: dict[str, Iterator[DataEnvelope]],
    primary_key: str = "primary",
) -> Iterator[DataEnvelope]:
    """Primary source payload + secondary sources inject into metadata.

    The sources dict must contain a key matching `primary_key`.
    Each secondary source's data is injected into metadata[secondary_name].
    If secondary yields structured data (dict), merge its keys directly.
    """
    primary_iter = sources[primary_key]
    secondary_iters = {
        k: v for k, v in sources.items() if k != primary_key
    }

    # Collect all secondary envelopes upfront
    secondary_data: dict[str, list[DataEnvelope]] = {}
    for name, it in secondary_iters.items():
        secondary_data[name] = list(it)

    for envelope in primary_iter:
        enriched = copy.deepcopy(envelope)
        for name, envelopes in secondary_data.items():
            if len(envelopes) == 1:
                sec = envelopes[0]
                if isinstance(sec.data, dict):
                    enriched.metadata.update(sec.data)
                else:
                    enriched.metadata[name] = sec.data
            elif envelopes:
                enriched.metadata[name] = [e.data for e in envelopes]
        yield enriched


def merge_batch(
    sources: dict[str, Iterator[DataEnvelope]] | list[Iterator[DataEnvelope]],
) -> Iterator[DataEnvelope]:
    """Collect all envelopes from all sources into a flat sequence."""
    if isinstance(sources, dict):
        iterators = sources.values()
    else:
        iterators = sources
    for it in iterators:
        yield from it


def merge_zip(
    sources: dict[str, Iterator[DataEnvelope]] | list[Iterator[DataEnvelope]],
) -> Iterator[DataEnvelope]:
    """Pair envelopes 1:1 across sources, stop at shortest.

    Yields merged envelopes where the first source provides the base
    and additional sources' data is merged into metadata.
    """
    if isinstance(sources, dict):
        names = list(sources.keys())
        iterators = [sources[n] for n in names]
    else:
        names = [f"source_{i}" for i in range(len(sources))]
        iterators = list(sources)

    exhausted = False
    while not exhausted:
        current: list[DataEnvelope] = []
        for it in iterators:
            try:
                current.append(next(it))
            except StopIteration:
                if current:
                    logger.warning(
                        "ZIP merge: sources have unequal lengths, "
                        "stopping at shortest"
                    )
                exhausted = True
                break

        if exhausted or not current:
            break

        # Use first source as base, merge others into metadata
        base = copy.deepcopy(current[0])
        for i, env in enumerate(current[1:], start=1):
            key = names[i] if isinstance(sources, dict) else f"source_{i}"
            base.metadata[key] = env.data
        yield base


def merge_concat(
    sources: dict[str, Iterator[DataEnvelope]] | list[Iterator[DataEnvelope]],
) -> Iterator[DataEnvelope]:
    """Yield all envelopes from source 1, then source 2, etc."""
    if isinstance(sources, dict):
        iterators = sources.values()
    else:
        iterators = sources
    for it in iterators:
        yield from it


def apply_merge(
    strategy: MergeStrategy,
    sources: dict[str, Iterator[DataEnvelope]] | list[Iterator[DataEnvelope]],
) -> Iterator[DataEnvelope]:
    """Apply a merge strategy to multiple source iterators."""
    match strategy:
        case MergeStrategy.ENRICH:
            if not isinstance(sources, dict):
                raise ValueError("ENRICH strategy requires a dict of sources")
            if "primary" not in sources:
                raise ValueError(
                    "ENRICH strategy requires a 'primary' key in sources"
                )
            yield from merge_enrich(sources)
        case MergeStrategy.BATCH:
            yield from merge_batch(sources)
        case MergeStrategy.ZIP:
            yield from merge_zip(sources)
        case MergeStrategy.CONCAT:
            yield from merge_concat(sources)
