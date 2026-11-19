"""A SQL API answer read as it streams in, one row at a time.

Snowflake, not the connector, decides how large a result partition is (its
docs show one of 76 MB uncompressed), and parsing a partition whole costs
about three times its bytes. So an answer's body is never parsed whole
(#46 review seq 14088 M2): it is read :data:`READ_CHUNK` bytes at a time,
and split as it arrives.

* ``data`` (``jsonv2``: an array of rows, each an array of strings and
  nulls) is read a row at a time (:meth:`Body.rows`): each row is parsed on
  its own, at most :data:`ROW_BYTES` of JSON, so a partition costs one chunk
  and one row whatever its size, the first one (inside the status answer)
  included. A caller that stops taking rows stops reading.
* Every other key's value (``resultSetMetaData``, ``code``, ...) is parsed
  whole (:meth:`Body.value`), all of them together at most
  :data:`HEAD_BYTES`.

The scan works on the body's bytes: JSON's structural characters are ASCII,
and no byte of a multi-byte UTF-8 character is, so a ``"``, ``\\``, ``[``
or ``]`` byte is always that character. Only a row's own bytes are ever
decoded.

What isn't such a body is :class:`BadBody` (its message never quotes the
body); a row over :data:`ROW_BYTES` is :class:`RowOverCap`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

#: Bytes read from a body at a time.
READ_CHUNK = 64 * 1024
#: The most JSON one row may take (its ``jsonv2`` array): a row over it is
#: :class:`RowOverCap` (the caller's ``RowTooLarge``, a 422). As BigQuery's
#: read cap, 8 MiB: parsing a row costs up to about 9 times its bytes when
#: an astral character (an emoji) makes Python hold its text at 4 bytes a
#: character.
ROW_BYTES = 8 * 1024 * 1024
#: The most JSON an answer's keys other than ``data`` may take together
#: (``resultSetMetaData`` with its ``rowType`` and ``partitionInfo``, the
#: code, the message, ...).
HEAD_BYTES = 8 * 1024 * 1024

_BLANK = re.compile(rb"[ \t\r\n]*+")
# A jsonv2 row so far: "[", then strings (whole), nulls, numbers, commas and
# blanks; never a nested array or object. Possessive, so a row cut off at
# the buffer's end costs one pass, not a backtracking search.
_ROW = re.compile(rb'\[(?:[^"\[\]{}]++|"(?:[^"\\]++|\\.)*+")*+', re.DOTALL)
_DECODER = json.JSONDecoder()
_CLOSE_ROW = ord("]")
_QUOTE = ord('"')
# Rows passed through unparsed (Body.pass_rows): between strings, anything
# but a quote or a bracket; inside one, up to its closing quote (an escape
# is two bytes, so an escaped quote never ends it).
_PASS_OUT = re.compile(rb'[^"\[\]]*+')
_PASS_IN = re.compile(rb'(?:[^"\\]++|\\.)*+', re.DOTALL)


class BadBody(Exception):
    """The body is not a JSON object of the SQL API's shape, or ends early.
    The message says how, never what the body holds."""


class RowOverCap(Exception):
    """A row whose JSON is over ``limit`` (:data:`ROW_BYTES`; ``read``: the
    bytes of it read, all of it or at least that many)."""

    def __init__(self, read: int, limit: int) -> None:
        super().__init__(f"a row of over {limit} bytes of JSON")
        self.read = read
        self.limit = limit


class Body:
    """One streamed JSON object body (``chunks``: its bytes, as
    ``iter_content`` gives them). :meth:`members` yields its keys in order;
    after each, read its value with :meth:`value` or :meth:`rows` (or
    :meth:`pass_rows`, which may also finish what :meth:`rows` began) before
    asking for the next key."""

    def __init__(self, chunks: Iterator[bytes]) -> None:
        self._chunks = chunks
        self._buf = bytearray()
        self._pos = 0
        self._eof = False
        self._head = 0
        #: The body's bytes read so far.
        self.bytes_read = 0
        #: The rows of ``data`` begun so far (a row over the cap included).
        self.rows_read = 0
        #: Inside ``data``'s array: :meth:`rows` has read its "[" and not
        #: its "]".
        self._in_rows = False

    # -- reading -------------------------------------------------------------

    def _fill(self, want: int) -> bool:
        """Drop what was consumed, then read until ``want`` bytes are
        buffered; False when the body ended first."""
        if self._pos:
            del self._buf[: self._pos]
            self._pos = 0
        while len(self._buf) < want:
            chunk = next(self._chunks, None)
            if chunk is None:
                self._eof = True
                return False
            self._buf += chunk
            self.bytes_read += len(chunk)
        return True

    def _more(self, limit: int) -> bool:
        """Read past what is buffered: as much again (at least a chunk, and
        no more than ``limit`` and a byte in all), so an item scanned again
        from its start after each read costs at most twice its bytes."""
        pending = len(self._buf) - self._pos
        return self._fill(min(pending + max(pending, READ_CHUNK), limit + 1))

    def _peek(self) -> int | None:
        """The next byte that is not a blank (not consumed), None at the end."""
        while True:
            self._pos = _BLANK.match(self._buf, self._pos).end()  # type: ignore[union-attr]
            if self._pos < len(self._buf):
                return self._buf[self._pos]
            if self._eof or not self._fill(1):
                return None

    def _expect(self, char: bytes, where: str) -> None:
        if self._peek() != char[0]:
            raise BadBody(
                f"the body is not a JSON object of the expected shape ({where})"
            )
        self._pos += 1

    def _decoded(self, limit: int, head: bool) -> Any:
        """The JSON value that starts here, read whole (at most ``limit``
        bytes of it)."""
        if self._peek() is None:
            raise BadBody("the body ends before a value")
        while True:
            pending = len(self._buf) - self._pos
            # surrogateescape: a UTF-8 character cut off at the buffer's end
            # stays bytes, and the bytes a value took are counted exactly.
            text = bytes(self._buf[self._pos :]).decode("utf-8", "surrogateescape")
            try:
                value, end = _DECODER.raw_decode(text)
            except json.JSONDecodeError:
                end = -1
            # A value that reaches the buffer's end may go on (a number):
            # complete only at the body's end.
            if end >= 0 and (end < len(text) or self._eof):
                used = len(text[:end].encode("utf-8", "surrogateescape"))
                if used > limit:
                    raise BadBody(f"a value in the body is over {limit} bytes")
                self._pos += used
                if head:
                    self._head += used
                    if self._head > HEAD_BYTES:
                        raise BadBody(
                            f"the answer's keys other than data are over {HEAD_BYTES} bytes"
                        )
                return value
            if self._eof:
                raise BadBody("the body ends inside a value, or it is not JSON")
            if pending > limit:
                raise BadBody(f"a value in the body is over {limit} bytes")
            self._more(limit)

    # -- the object ----------------------------------------------------------

    def members(self) -> Iterator[str]:
        """The object's keys, in order. Each value is read by the caller
        before it asks for the next key; after the last, nothing but blanks
        may follow."""
        self._expect(b"{", "an object")
        first = True
        while True:
            char = self._peek()
            if char == ord("}"):
                self._pos += 1
                if self._peek() is not None:
                    raise BadBody("the body goes on after its object")
                return
            if not first:
                self._expect(b",", "a comma between keys")
            first = False
            key = self._decoded(HEAD_BYTES, head=True)
            if not isinstance(key, str):
                raise BadBody("the body is not a JSON object (a key is not a string)")
            self._expect(b":", "a colon after a key")
            yield key

    def value(self) -> Any:
        """The current key's value, parsed whole (within :data:`HEAD_BYTES`)."""
        return self._decoded(HEAD_BYTES, head=True)

    def rows(self) -> Iterator[Any]:
        """The current key's value, an array of rows, one row at a time (a
        ``jsonv2`` row: a list of strings and nulls). A caller that stops
        iterating stops reading; the body can't be read on after that."""
        if self._peek() == ord("n"):  # "data": null
            if self._decoded(4, head=True) is not None:
                raise BadBody("data is not an array")
            return
        self._expect(b"[", "data is an array")
        self._in_rows = True
        first = True
        while True:
            char = self._peek()
            if char is None:
                raise BadBody("the body ends inside data")
            if char == _CLOSE_ROW:
                self._pos += 1
                self._in_rows = False
                return
            if not first:
                self._expect(b",", "a comma between rows")
                char = self._peek()
            first = False
            self.rows_read += 1
            if char == ord("["):
                yield self._row()
            else:
                # Not an array (a null row, say): read whole, bounded.
                yield self._decoded(ROW_BYTES, head=False)

    def pass_rows(self) -> None:
        """Read through the current key's rows, or the rest of them once
        :meth:`rows` has begun (a caller that stopped taking rows), keeping
        and parsing none, WHATEVER their size: no row cap applies, since the
        caller gets none of them (#46 review seq 14138 nit d). Their bytes
        are scanned for where ``data`` ends and let go as they arrive, so at
        most about a chunk is buffered. Only that end is checked: a row that
        isn't ``jsonv2`` passes. :attr:`rows_read` doesn't count them."""
        if not self._in_rows:
            if self._peek() == ord("n"):  # "data": null
                if self._decoded(4, head=True) is not None:
                    raise BadBody("data is not an array")
                return
            self._expect(b"[", "data is an array")
            self._in_rows = True
        depth = 1  # inside data's array
        while True:
            self._pos = _PASS_OUT.match(self._buf, self._pos).end()  # type: ignore[union-attr]
            if self._pos == len(self._buf):
                if not self._fill(1):
                    raise BadBody("the body ends inside data")
                continue
            char = self._buf[self._pos]
            self._pos += 1
            if char == _QUOTE:
                while True:
                    self._pos = _PASS_IN.match(self._buf, self._pos).end()  # type: ignore[union-attr]
                    if self._pos < len(self._buf) and self._buf[self._pos] == _QUOTE:
                        self._pos += 1
                        break
                    # The buffer ends inside the string, or on half an escape.
                    if not self._fill(len(self._buf) - self._pos + 1):
                        raise BadBody("the body ends inside data")
            elif char == _CLOSE_ROW:
                depth -= 1
                if depth == 0:
                    self._in_rows = False
                    return
            else:  # "["
                depth += 1

    def _row(self) -> list[Any]:
        while True:
            end = _ROW.match(self._buf, self._pos).end()  # type: ignore[union-attr]
            if end < len(self._buf):
                after = self._buf[end]
                if after == _CLOSE_ROW:
                    if end + 1 - self._pos > ROW_BYTES:
                        raise RowOverCap(end + 1 - self._pos, ROW_BYTES)
                    raw = bytes(self._buf[self._pos : end + 1])
                    self._pos = end + 1
                    try:
                        row = json.loads(raw)
                    except ValueError:
                        raise BadBody("a row in data is not JSON") from None
                    if not isinstance(row, list):  # pragma: no cover - "[" ... "]"
                        raise BadBody("a row in data is not an array")
                    return row
                if after != _QUOTE:
                    # A nested array or object, or a stray bracket: not jsonv2.
                    raise BadBody("a row in data is not an array of strings and nulls")
                # Else a string runs past what is buffered.
            pending = len(self._buf) - self._pos
            if pending > ROW_BYTES:
                raise RowOverCap(pending, ROW_BYTES)
            if self._eof:
                raise BadBody("the body ends inside a row")
            # At the body's end this reads what is left: the next pass sees it.
            self._more(ROW_BYTES)


__all__ = [
    "HEAD_BYTES",
    "READ_CHUNK",
    "ROW_BYTES",
    "BadBody",
    "Body",
    "RowOverCap",
]
