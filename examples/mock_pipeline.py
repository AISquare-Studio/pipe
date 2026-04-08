#!/usr/bin/env python3
"""Example: MockSource -> MockSink pipeline.

Run after installing:
    pip install -e .
    python examples/mock_pipeline.py
"""

from aisquare.pipe import Pipeline
from aisquare.pipe.testing.mock_connectors import MockSink, MockSource


def main():
    # Create a source that generates 5 text envelopes
    source = MockSource(count=5)

    # Create a sink that collects envelopes in memory
    sink = MockSink()

    # Build the pipeline
    pipeline = Pipeline(source=source, sink=sink)

    # Dry run — check compatibility first
    report = pipeline.dry_run({})
    print(f"Compatible: {report.compatible}")
    print(f"Match level: {report.match_level.name}")
    if report.warnings:
        print(f"Warnings: {report.warnings}")
    print()

    # Run the pipeline
    result = pipeline.run({})

    print(f"Pipeline complete!")
    print(f"  Success: {result.success_count}")
    print(f"  Failed:  {result.failure_count}")
    print()

    # Inspect what the sink received
    print("Envelopes received by sink:")
    for i, env in enumerate(sink.received):
        print(f"  [{i}] type={env.content_type} data={env.data!r} meta={env.metadata}")


if __name__ == "__main__":
    main()
