"""Dataset and table references as callers write them.

``dataset`` or ``project.dataset``; ``dataset.table`` or
``project.dataset.table`` (backticks around the whole reference are
dropped). A reference without a project means the tenant's ``project_id``.
Naming another project does not widen anything: the service account reads
only what IAM grants it, and every job still runs in ``project_id``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from aisquare_pipe_bigquery.credentials import PROJECT_ID

#: Dataset ids: letters, digits and underscores, up to 1024 characters.
DATASET_ID = re.compile(r"^[A-Za-z0-9_]{1,1024}$")
#: Table ids: letters (any script), digits, underscores, dashes and spaces,
#: up to 1024 characters. No dots or wildcards: one table.
TABLE_ID = re.compile(r"^[\w\- ]{1,1024}$")
MAX_REF_CHARS = 2200


@dataclass(frozen=True)
class TableRef:
    """``project.dataset.table``; ``project`` None means the job's project."""

    project: str | None
    dataset: str
    table: str

    def qualified(self, default_project: str) -> str:
        return f"{self.project or default_project}.{self.dataset}.{self.table}"


class BadReference(ValueError):
    """A dataset or table reference that names no single dataset or table."""


def _unquote(text: object, what: str) -> str:
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_REF_CHARS:
        raise BadReference(f"{what} must be a non-empty reference")
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] == "`":
        text = text[1:-1]
    if not text.isprintable():
        raise BadReference(f"{what} holds control characters")
    return text


def _project(value: str, what: str) -> str:
    if not PROJECT_ID.match(value):
        raise BadReference(f"{what} names {value!r}, which is not a project id")
    return value


def parse_dataset(text: object, what: str = "dataset") -> tuple[str | None, str]:
    """``(project or None, dataset)`` from ``dataset`` or ``project.dataset``."""
    value = _unquote(text, what)
    project, _, dataset = value.rpartition(".")
    if not DATASET_ID.match(dataset):
        raise BadReference(
            f"{what} must be 'dataset' or 'project.dataset' (dataset ids are letters, "
            "digits and underscores)"
        )
    return (_project(project, what) if project else None), dataset


def parse_table(text: object, what: str = "table") -> TableRef:
    """A :class:`TableRef` from ``dataset.table`` or ``project.dataset.table``."""
    value = _unquote(text, what)
    head, _, table = value.rpartition(".")
    if not head or not TABLE_ID.match(table):
        raise BadReference(
            f"{what} must be 'dataset.table' or 'project.dataset.table' (one table, "
            "no wildcard)"
        )
    project, dataset = parse_dataset(head, what)
    return TableRef(project, dataset, table)


__all__ = [
    "DATASET_ID",
    "TABLE_ID",
    "BadReference",
    "TableRef",
    "parse_dataset",
    "parse_table",
]
