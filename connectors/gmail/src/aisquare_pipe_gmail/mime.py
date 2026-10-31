"""MIME both ways: RFC 2822 messages built for send/draft/reply, and Gmail's
``payload`` part tree read back into headers, text and attachments.

Building uses the standard library's :class:`email.message.EmailMessage`
under the SMTP policy: non-ASCII subjects and names are RFC 2047-encoded,
bodies get a fitting transfer encoding, and an HTML body also carries a
plain-text alternative. Header values with a CR or LF are refused (header
injection), as are recipients that are not ``name@domain`` addresses.

Reading walks ``payload.parts`` recursively. Gmail hands each part's body
already transfer-decoded, base64url-encoded, in the part's charset; text is
decoded with that charset (UTF-8 when unnamed or unknown). A part with a
filename is an attachment — its bytes come from ``GMAIL_GET_ATTACHMENT`` with
its ``attachmentId`` — and is listed, not inlined.
"""

from __future__ import annotations

import codecs
import email.policy
import email.utils
import html
import mimetypes
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from email.headerregistry import Address
from email.message import EmailMessage
from html.parser import HTMLParser
from typing import Any

from aisquare_pipe_gmail.rest import decode_base64url

#: Longest ``messageText`` returned per message (characters).
TEXT_MAX = 20_000

_ADDR_SPEC = re.compile(r"^[^@\s<>()\[\]\\,;:\"]+@[^@\s<>()\[\]\\,;:\"]+$")
_BLOCK_TAGS = frozenset(
    {
        "p", "div", "br", "li", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6",
        "blockquote", "pre", "hr", "section", "article", "header", "footer", "ul", "ol",
    }
)
_SKIP_TAGS = frozenset({"script", "style", "head", "title"})


@dataclass(frozen=True)
class Attachment:
    """A file to attach: its name, MIME type and bytes."""

    name: str
    mimetype: str
    content: bytes


# --------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------


def check_header(value: str, what: str) -> str:
    """``value`` for a header; ValueError when it carries a line break."""
    if "\r" in value or "\n" in value:
        raise ValueError(f"{what} must not contain line breaks")
    return value


def parse_addresses(values: Iterable[str], what: str) -> list[Address]:
    """Each value is one or more addresses (``a@x.co``, ``Ann <a@x.co>``,
    ``a@x.co, b@y.co``); ValueError for anything that is not an address."""
    out: list[Address] = []
    for value in values:
        text = check_header(str(value), what).strip()
        if not text:
            continue
        for name, addr in email.utils.getaddresses([text]):
            if not _ADDR_SPEC.match(addr):
                raise ValueError(f"{what} has something that is not an email address")
            local, _, domain = addr.rpartition("@")
            out.append(Address(display_name=name, username=local, domain=domain))
    return out


def build_message(
    *,
    to: Iterable[Address],
    cc: Iterable[Address] = (),
    bcc: Iterable[Address] = (),
    subject: str = "",
    body: str = "",
    is_html: bool = False,
    sender: Address | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    attachments: Iterable[Attachment] = (),
) -> bytes:
    """An RFC 2822 message (CRLF line endings) ready for Gmail's ``raw``."""
    msg = EmailMessage(policy=email.policy.SMTP)
    if sender is not None:
        msg["From"] = sender
    for name, addresses in (("To", tuple(to)), ("Cc", tuple(cc)), ("Bcc", tuple(bcc))):
        if addresses:
            msg[name] = addresses
    msg["Subject"] = check_header(subject, "subject")
    if in_reply_to:
        msg["In-Reply-To"] = check_header(in_reply_to, "In-Reply-To")
    if references:
        msg["References"] = check_header(references, "References")
    if is_html:
        msg.set_content(html_to_text(body) or " ")
        msg.add_alternative(body, subtype="html")
    else:
        msg.set_content(body)
    for item in attachments:
        maintype, _, subtype = item.mimetype.partition("/")
        if not maintype or not subtype:
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(item.content, maintype=maintype, subtype=subtype, filename=item.name)
    return msg.as_bytes()


def guess_mimetype(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def safe_filename(name: str, fallback: str = "attachment") -> str:
    """A file name with no directory part and no control characters."""
    text = re.split(r"[\\/]", str(name))[-1]
    text = "".join(ch for ch in text if ch.isprintable()).strip()
    return text if text not in ("", ".", "..") else fallback


def reply_subject(subject: str) -> str:
    """``Re: <subject>``, without stacking ``Re:`` prefixes."""
    text = subject.strip()
    return text if re.match(r"(?i)^re\s*:", text) else f"Re: {text}".rstrip()


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def headers_of(part: Mapping[str, Any]) -> dict[str, str]:
    """A part's headers by lower-cased name (the first of repeated ones)."""
    out: dict[str, str] = {}
    for header in part.get("headers") or []:
        if isinstance(header, Mapping) and isinstance(header.get("name"), str):
            out.setdefault(header["name"].lower(), str(header.get("value") or ""))
    return out


def walk(part: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    """Every part of a payload, depth first (the payload itself first)."""
    yield part
    for child in part.get("parts") or []:
        if isinstance(child, Mapping):
            yield from walk(child)


def _charset(part: Mapping[str, Any]) -> str:
    content_type = headers_of(part).get("content-type", "")
    match = re.search(r"charset\s*=\s*\"?([^\";\s]+)", content_type, re.IGNORECASE)
    name = match.group(1) if match else "utf-8"
    try:
        codecs.lookup(name)
    except LookupError:
        return "utf-8"
    return name


def part_text(part: Mapping[str, Any]) -> str | None:
    """A text part's decoded body (None when it has none inline)."""
    data = (part.get("body") or {}).get("data")
    if not isinstance(data, str) or not data:
        return None
    try:
        raw = decode_base64url(data)
    except ValueError:
        return None
    return raw.decode(_charset(part), errors="replace")


def attachments_of(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The payload's attachments: filename, mimeType, size, attachmentId, partId."""
    out: list[dict[str, Any]] = []
    for part in walk(payload):
        filename = part.get("filename")
        if not filename:
            continue
        body = part.get("body") or {}
        item: dict[str, Any] = {
            "filename": str(filename),
            "mimeType": str(part.get("mimeType") or guess_mimetype(str(filename))),
            "size": body.get("size") if isinstance(body.get("size"), int) else None,
            "attachmentId": body.get("attachmentId"),
            "partId": part.get("partId"),
        }
        disposition = headers_of(part).get("content-disposition", "")
        if disposition.lower().startswith("inline"):
            item["inline"] = True
        out.append(item)
    return out


def body_text(payload: Mapping[str, Any]) -> tuple[str, bool]:
    """The message's readable text — its ``text/plain`` parts, else its
    ``text/html`` ones as text — capped at :data:`TEXT_MAX` characters.
    Returns ``(text, truncated)``. Attachment parts are skipped."""
    plain: list[str] = []
    rich: list[str] = []
    for part in walk(payload):
        if part.get("filename"):
            continue
        mime = str(part.get("mimeType") or "").lower()
        if mime not in ("text/plain", "text/html"):
            continue
        text = part_text(part)
        if text is None:
            continue
        (plain if mime == "text/plain" else rich).append(text)
    text = "\n".join(plain) if plain else "\n".join(html_to_text(t) for t in rich)
    text = text.replace("\r\n", "\n").strip()
    if len(text) > TEXT_MAX:
        return text[:TEXT_MAX], True
    return text, False


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.chunks.append(data)


def html_to_text(markup: str) -> str:
    """Readable text from HTML: scripts and styles dropped, block elements
    on their own lines, entities decoded, whitespace (non-breaking spaces
    included) collapsed, blank lines dropped."""
    parser = _TextExtractor()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # noqa: BLE001 — malformed markup degrades to tag stripping
        return html.unescape(re.sub(r"<[^>]*>", " ", markup)).strip()
    lines = (" ".join(line.split()) for line in "".join(parser.chunks).splitlines())
    return "\n".join(line for line in lines if line)
