"""Snowflake object names, read as Snowflake reads them, and written back quoted.

A name is dotted parts (``db.schema.table``). Each part is either
* unquoted: a letter or ``_``, then letters, digits, ``_`` or ``$``, at most
  255 characters; case-insensitive, so it resolves upper-cased
  (``policies`` -> ``POLICIES``); or
* double-quoted: any characters but control characters, ``""`` for a quote;
  case-sensitive, resolved as written (``"Policies"`` -> ``Policies``).

Resolved names are what INFORMATION_SCHEMA stores and what the SQL API's
``database``/``schema``/``role``/``warehouse`` fields expect (they are
case-sensitive). :func:`quote` writes one back as a quoted identifier, so a
name the connector puts into SQL is always one quoted token.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_NAME_CHARS = 255
MAX_REF_CHARS = 1000
_UNQUOTED = re.compile(r"[A-Za-z_][A-Za-z0-9_$]{0,254}")


class BadReference(ValueError):
    """Not a Snowflake object name of the expected shape."""


def parse_parts(text: object, what: str, *, max_parts: int) -> list[str]:
    """``text`` as 1 to ``max_parts`` resolved name parts."""
    if not isinstance(text, str) or not text.strip():
        raise BadReference(f"{what} is missing")
    if len(text) > MAX_REF_CHARS:
        raise BadReference(f"{what} is longer than {MAX_REF_CHARS} characters")
    source = text.strip()
    parts: list[str] = []
    i, n = 0, len(source)
    while True:
        if i < n and source[i] == '"':
            j, chars = i + 1, []
            while True:
                if j >= n:
                    raise BadReference(f"{what} has an unterminated quoted name")
                if source[j] == '"':
                    if j + 1 < n and source[j + 1] == '"':
                        chars.append('"')
                        j += 2
                        continue
                    break
                chars.append(source[j])
                j += 1
            name = "".join(chars)
            if not name or len(name) > MAX_NAME_CHARS or not name.isprintable():
                raise BadReference(f"{what} has an empty or unprintable quoted name")
            parts.append(name)
            i = j + 1
        else:
            match = _UNQUOTED.match(source, i)
            if match is None:
                raise BadReference(
                    f"{what} must be a Snowflake name such as DB.SCHEMA.TABLE (a name with "
                    'other characters goes in double quotes: "my table")'
                )
            parts.append(match.group(0).upper())
            i = match.end()
        if i == n:
            break
        if source[i] != ".":
            raise BadReference(
                f"{what} must be a Snowflake name such as DB.SCHEMA.TABLE (a name with "
                'other characters goes in double quotes: "my table")'
            )
        i += 1
        if len(parts) >= max_parts:
            raise BadReference(f"{what} has more than {max_parts} dotted parts")
    return parts


def parse_identifier(text: object, what: str) -> str:
    """One resolved name (no dots outside quotes)."""
    (name,) = parse_parts(text, what, max_parts=1)
    return name


def quote(name: str) -> str:
    """``name`` as ONE quoted Snowflake identifier."""
    return '"' + name.replace('"', '""') + '"'


def shown(name: str) -> str:
    """``name`` as a person would type it: bare when it reads back the same
    unquoted, else quoted."""
    if _UNQUOTED.fullmatch(name) and name == name.upper():
        return name
    return quote(name)


@dataclass(frozen=True)
class SchemaRef:
    database: str
    schema: str

    def sql(self) -> str:
        return f"{quote(self.database)}.{quote(self.schema)}"

    def display(self) -> str:
        return f"{shown(self.database)}.{shown(self.schema)}"


@dataclass(frozen=True)
class TableRef:
    database: str
    schema: str
    name: str

    def sql(self) -> str:
        return f"{quote(self.database)}.{quote(self.schema)}.{quote(self.name)}"

    def display(self) -> str:
        return f"{shown(self.database)}.{shown(self.schema)}.{shown(self.name)}"


def parse_table(
    text: object, what: str, *, database: str | None, schema: str | None
) -> TableRef:
    """``table``, ``schema.table`` or ``db.schema.table``; the missing parts
    come from ``database``/``schema`` (already resolved), else
    :class:`BadReference`."""
    parts = parse_parts(text, what, max_parts=3)
    if len(parts) == 3:
        return TableRef(*parts)
    if len(parts) == 2:
        if database is None:
            raise BadReference(
                f"{what} needs its database (DB.SCHEMA.TABLE): no default database is "
                "configured"
            )
        return TableRef(database, parts[0], parts[1])
    if database is None or schema is None:
        raise BadReference(
            f"{what} needs its database and schema (DB.SCHEMA.TABLE): no default "
            "database and schema are configured"
        )
    return TableRef(database, schema, parts[0])


def parse_schema(text: object, what: str, *, database: str | None) -> SchemaRef:
    """``schema`` or ``db.schema``; a missing database comes from
    ``database``, else :class:`BadReference`."""
    parts = parse_parts(text, what, max_parts=2)
    if len(parts) == 2:
        return SchemaRef(*parts)
    if database is None:
        raise BadReference(
            f"{what} needs its database (DB.SCHEMA): no default database is configured"
        )
    return SchemaRef(database, parts[0])


__all__ = [
    "MAX_REF_CHARS",
    "BadReference",
    "SchemaRef",
    "TableRef",
    "parse_identifier",
    "parse_parts",
    "parse_schema",
    "parse_table",
    "quote",
    "shown",
]
