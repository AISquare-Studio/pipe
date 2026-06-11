"""Factories for toolkit-pinned Composio connector classes.

``composio_source("gmail")`` returns a ``ComposioSource`` subclass named
``composio-gmail-source`` that only executes ``GMAIL_*`` tools. The factory
sets class attributes only — all behaviour (including the pin enforcement)
lives in the base classes, so the produced classes remain zero-arg
instantiable and compliance-suite compatible.

These classes are deliberately not registered as entry points: entry points
are static strings and ``pipe list`` instantiates each one eagerly, while
Composio has ~500 toolkits. Build them programmatically on demand.
"""

from __future__ import annotations

import re

from aisquare_pipe_composio.connector import (
    COMPOSIO_DOCS_URL,
    ComposioSink,
    ComposioSource,
)

_TOOLKIT_SLUG_RE = re.compile(r"^[a-z0-9_-]+$")


def _validate_toolkit_arg(toolkit: str) -> str:
    slug = str(toolkit).strip().lower()
    if not slug or not _TOOLKIT_SLUG_RE.match(slug):
        raise ValueError(
            f"toolkit must be a Composio toolkit slug (e.g. 'gmail'), got {toolkit!r}"
        )
    return slug


def _class_name(slug: str, suffix: str) -> str:
    camel = "".join(part.capitalize() for part in re.split(r"[^a-z0-9]+", slug) if part)
    return f"Composio{camel}{suffix}"


def composio_source(toolkit: str) -> type[ComposioSource]:
    """Build a ComposioSource subclass pinned to one toolkit."""
    slug = _validate_toolkit_arg(toolkit)
    return type(
        _class_name(slug, "Source"),
        (ComposioSource,),
        {
            "name": f"composio-{slug}-source",
            "description": f"Execute Composio '{slug}' tools and yield their results",
            "docs_url": f"{COMPOSIO_DOCS_URL}/toolkits/{slug}",
            "toolkit": slug,
            "__module__": __name__,
            "__doc__": f"Toolkit-pinned ComposioSource for '{slug}'.",
        },
    )


def composio_sink(toolkit: str) -> type[ComposioSink]:
    """Build a ComposioSink subclass pinned to one toolkit."""
    slug = _validate_toolkit_arg(toolkit)
    return type(
        _class_name(slug, "Sink"),
        (ComposioSink,),
        {
            "name": f"composio-{slug}-sink",
            "description": f"Execute Composio '{slug}' tools as write actions",
            "docs_url": f"{COMPOSIO_DOCS_URL}/toolkits/{slug}",
            "toolkit": slug,
            "__module__": __name__,
            "__doc__": f"Toolkit-pinned ComposioSink for '{slug}'.",
        },
    )
