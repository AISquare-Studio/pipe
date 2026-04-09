"""CLI entrypoint for aisquare.pipe."""

from __future__ import annotations

import importlib
import inspect
import json
import os
import sys

import click

from aisquare.pipe.core.connector import SinkConnector, SourceConnector
from aisquare.pipe.core.envelope import PullParams, PushParams
from aisquare.pipe.core.pipeline import Pipeline
from aisquare.pipe.core.registry import discover_connectors, get_connector
from aisquare.pipe.core.types import TypeMatcher


@click.group()
def cli() -> None:
    """aisquare.pipe — Universal anything-to-anything connector framework."""


@cli.command("list")
def list_connectors() -> None:
    """List all installed connectors."""
    connectors = discover_connectors()
    if not connectors:
        click.echo("No connectors found. Install connector plugins or run: pip install -e .")
        return

    click.echo(f"{'Name':<25} {'Version':<10} {'Type':<10} {'Auth':<10}")
    click.echo("-" * 55)
    for name, cls in sorted(connectors.items()):
        inst = cls()
        is_source = isinstance(inst, SourceConnector)
        is_sink = isinstance(inst, SinkConnector)
        if is_source and is_sink:
            ctype = "duplex"
        elif is_source:
            ctype = "source"
        else:
            ctype = "sink"
        click.echo(f"{name:<25} {inst.version:<10} {ctype:<10} {inst.auth_type.value:<10}")


@cli.command()
@click.argument("name")
def describe(name: str) -> None:
    """Show full details about a connector."""
    try:
        cls = get_connector(name)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    inst = cls()
    click.echo(f"Name:        {inst.name}")
    click.echo(f"Version:     {inst.version}")
    click.echo(f"Auth:        {inst.auth_type.value}")
    click.echo(f"Description: {inst.description or '(none)'}")

    if isinstance(inst, SourceConnector):
        click.echo(f"Type:        source")
        click.echo(f"Output:      {', '.join(inst.output_types)}")

    if isinstance(inst, SinkConnector):
        click.echo(f"Type:        sink")
        click.echo(f"Input:       {', '.join(inst.input_types)}")

    if inst.metadata_spec:
        click.echo("Metadata spec:")
        for key, spec in inst.metadata_spec.items():
            req = " (required)" if spec.required else ""
            click.echo(f"  {key}: {spec.type.__name__}{req} — {spec.description}")


@cli.command()
@click.argument("source_name")
@click.argument("sink_name")
def check(source_name: str, sink_name: str) -> None:
    """Check type compatibility between a source and a sink."""
    try:
        source_cls = get_connector(source_name)
        sink_cls = get_connector(sink_name)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    source = source_cls()
    sink = sink_cls()

    if not isinstance(source, SourceConnector):
        click.echo(f"Error: {source_name} is not a source connector", err=True)
        sys.exit(1)
    if not isinstance(sink, SinkConnector):
        click.echo(f"Error: {sink_name} is not a sink connector", err=True)
        sys.exit(1)

    matcher = TypeMatcher()
    click.echo(f"Checking: {source_name} -> {sink_name}\n")

    any_match = False
    for out_type in source.output_types:
        result = matcher.match(out_type, sink.input_types)
        status = "OK" if result.level.value > 0 else "FAIL"
        click.echo(f"  [{status}] {out_type} — {result.message}")
        if result.level.value > 0:
            any_match = True

    click.echo()
    if any_match:
        click.echo("Result: Compatible")
    else:
        click.echo("Result: Not compatible")


@cli.command()
@click.option("--source", "source_name", required=True, help="Source connector name")
@click.option("--sink", "sink_name", required=True, help="Sink connector name")
@click.option("--config", "config_path", required=True, help="Path to JSON config file")
@click.option(
    "--pull-params", "pull_params_json", default=None,
    help='JSON string of pull params, e.g. \'{"recursive": true, "extensions": [".pdf"]}\'',
)
@click.option(
    "--push-params", "push_params_json", default=None,
    help='JSON string of push params, e.g. \'{"target_path": "backup", "conflict": "overwrite"}\'',
)
def run(
    source_name: str,
    sink_name: str,
    config_path: str,
    pull_params_json: str | None,
    push_params_json: str | None,
) -> None:
    """Build and run a pipeline from CLI args."""
    try:
        source_cls = get_connector(source_name)
        sink_cls = get_connector(sink_name)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    try:
        with open(config_path) as f:
            config = json.load(f)
    except Exception as e:
        click.echo(f"Error reading config: {e}", err=True)
        sys.exit(1)

    pull_params = None
    if pull_params_json:
        try:
            pull_params = PullParams(params=json.loads(pull_params_json))
        except Exception as e:
            click.echo(f"Error parsing --pull-params: {e}", err=True)
            sys.exit(1)

    push_params = None
    if push_params_json:
        try:
            push_params = PushParams(params=json.loads(push_params_json))
        except Exception as e:
            click.echo(f"Error parsing --push-params: {e}", err=True)
            sys.exit(1)

    source = source_cls()
    sink = sink_cls()
    pipeline = Pipeline(source=source, sink=sink)
    result = pipeline.run(config, pull_params=pull_params, push_params=push_params)

    click.echo(f"Success: {result.success_count}")
    click.echo(f"Failed:  {result.failure_count}")
    if result.errors:
        click.echo("Errors:")
        for err in result.errors:
            click.echo(f"  [{err['envelope_index']}] {err['error']}")


@cli.command()
@click.argument("path")
def validate(path: str) -> None:
    """Load and run compliance tests against a connector file."""
    import unittest

    # Load the module from the file path
    spec = importlib.util.spec_from_file_location("connector_module", path)
    if spec is None or spec.loader is None:
        click.echo(f"Error: cannot load {path}", err=True)
        sys.exit(1)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # Find all connector classes in the module
    from aisquare.pipe.testing.compliance import connector_compliance_suite

    found = False
    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if (
            isinstance(attr, type)
            and (issubclass(attr, SourceConnector) or issubclass(attr, SinkConnector))
            and attr not in (SourceConnector, SinkConnector)
        ):
            found = True
            click.echo(f"\nValidating {attr_name}...")
            suite_cls = connector_compliance_suite(attr)
            suite = unittest.TestLoader().loadTestsFromTestCase(suite_cls)
            runner = unittest.TextTestRunner(verbosity=2)
            runner.run(suite)

    if not found:
        click.echo("No connector classes found in the file.", err=True)
        sys.exit(1)


@cli.command("new-connector")
@click.argument("name")
def new_connector(name: str) -> None:
    """Scaffold a new connector plugin project."""
    safe_name = name.replace("-", "_")
    project_dir = f"aisquare-pipe-{name}"

    if os.path.exists(project_dir):
        click.echo(f"Error: directory '{project_dir}' already exists", err=True)
        sys.exit(1)

    # Create directory structure
    os.makedirs(f"{project_dir}/src/aisquare_pipe_{safe_name}")
    os.makedirs(f"{project_dir}/tests")

    # pyproject.toml
    with open(f"{project_dir}/pyproject.toml", "w") as f:
        f.write(f'''[build-system]
requires = ["setuptools>=68.0"]
build-backend = "setuptools.build_meta"

[project]
name = "aisquare-pipe-{name}"
version = "0.1.0"
description = "aisquare.pipe connector for {name}"
requires-python = ">=3.11"
dependencies = ["aisquare-pipe>=0.1.0"]

[project.entry-points."aisquare_pipe.connectors"]
{name} = "aisquare_pipe_{safe_name}.connector:{safe_name.title()}Source"

[tool.setuptools.packages.find]
where = ["src"]
''')

    # Connector stub
    with open(f"{project_dir}/src/aisquare_pipe_{safe_name}/__init__.py", "w") as f:
        pass

    with open(f"{project_dir}/src/aisquare_pipe_{safe_name}/connector.py", "w") as f:
        f.write(f'''"""aisquare.pipe connector for {name}."""

from collections.abc import Iterator

from aisquare.pipe import (
    AuthType,
    DataEnvelope,
    MetaField,
    PullParams,
    PushResult,
    SourceConnector,
)


class {safe_name.title()}Source(SourceConnector):
    name = "{name}"
    version = "0.1.0"
    output_types = ["text/plain"]
    auth_type = AuthType.API_KEY
    description = "{name} source connector"

    def pull(self, config: dict, params: PullParams | None = None) -> Iterator[DataEnvelope]:
        raise NotImplementedError("TODO: implement pull")

    def validate_config(self, config: dict) -> bool:
        raise NotImplementedError("TODO: implement config validation")
''')

    # Test stub
    with open(f"{project_dir}/tests/__init__.py", "w") as f:
        pass

    with open(f"{project_dir}/tests/test_connector.py", "w") as f:
        f.write(f'''"""Tests for {name} connector."""

from aisquare.pipe.testing.compliance import connector_compliance_suite
from aisquare_pipe_{safe_name}.connector import {safe_name.title()}Source


class Test{safe_name.title()}Source(connector_compliance_suite({safe_name.title()}Source)):
    pass
''')

    click.echo(f"Created connector project: {project_dir}/")
    click.echo(f"  Edit: {project_dir}/src/aisquare_pipe_{safe_name}/connector.py")
    click.echo(f"  Test: cd {project_dir} && pip install -e . && pytest")


@cli.command("serve-mcp")
def serve_mcp() -> None:
    """Start the MCP server (not yet implemented)."""
    click.echo("MCP server not yet implemented")
    click.echo("This will be available in a future release.")
