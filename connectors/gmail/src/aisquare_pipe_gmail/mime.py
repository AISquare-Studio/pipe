"""MIME both ways: RFC 2822 messages built for send/draft/reply, and Gmail's
``payload`` part tree read back into headers, text and attachments.

Building uses the standard library's :class:`email.message.EmailMessage`
under the SMTP policy: non-ASCII subjects and names are RFC 2047-encoded,
bodies get a fitting transfer encoding, and an HTML body also carries a
plain-text alternative. Header values with a line break or another control
character are refused (header injection; a tab is whitespace and becomes a
space), as are recipients that are not ``name@domain`` addresses.
Encoded-words in display names, Subject, In-Reply-To and References are
decoded and the result checked the same way; In-Reply-To and References must
also be msg-ids.

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
from email.errors import HeaderParseError
from email.header import decode_header, make_header
from email.headerregistry import Address
from email.message import EmailMessage
from html.parser import HTMLParser
from typing import Any

from aisquare_pipe_gmail.rest import decode_base64url

#: Longest ``messageText`` returned per message (characters).
TEXT_MAX = 20_000

_ADDR_SPEC = re.compile(r"^[^@\s<>()\[\]\\,;:\"]+@[^@\s<>()\[\]\\,;:\"]+$")
#: Never allowed in a header value: every C0 control but HTAB (CR, LF, NUL, VT,
#: FF, the FS/GS/RS separators...), DEL, and the line breaks str.splitlines()
#: knows. HTAB is RFC 5322 whitespace: check_header turns it into a space.
_HEADER_UNSAFE = re.compile("[\x00-\x08\x0a-\x1f\x7f\x85\u2028\u2029]")
_ENCODED_WORD = re.compile(r"=\?[^?]*\?[bBqQ]\?[^?]*\?=")
#: An RFC 5322 msg-id, ``<id-left@id-right>``: dot-atom text on both sides (dots
#: anywhere, as real mailers write them), or a ``[...]`` literal on the right.
_ID_TEXT = r"[A-Za-z0-9!#$%&'*+\-/=?^_`{|}~.]+"
_MSG_ID = re.compile(rf"<{_ID_TEXT}@(?:{_ID_TEXT}|\[[!-Z^-~]*\])>")
#: Headers build_message sets, re-checked as the standard library parsed them.
_BUILT_HEADERS = ("From", "To", "Cc", "Bcc", "Subject", "In-Reply-To", "References")
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
    """``value`` for a header, each tab (HTAB, whitespace in RFC 5322) turned
    into a single space; ValueError when it carries a line break (CR, LF or a
    Unicode line separator), any other C0 control character, or DEL."""
    if _HEADER_UNSAFE.search(value):
        raise ValueError(f"{what} must not contain line breaks or control characters")
    return value.replace("\t", " ")


def decode_words(value: str, what: str) -> str:
    """``value`` checked (:func:`check_header`), with any RFC 2047
    encoded-words decoded and the decoded text checked again. ValueError when
    it can't be decoded or is still encoded after one decoding (double
    encoding).

    The standard library decodes encoded-words when it sets a Subject,
    In-Reply-To, References or display name, and on Pythons without the
    CVE-2024-6923 fix (3.11.0-3.11.9, 3.12.0-3.12.4) its generator writes a
    decoded CRLF out as a real line break: ``=?utf-8?b?...?=`` could add a
    Bcc. So the decoded text is what gets checked and used."""
    value = check_header(value, what)
    if "=?" not in value:
        return value
    try:
        decoded = str(make_header(decode_header(value)))
    except (HeaderParseError, LookupError, UnicodeError, ValueError):
        raise ValueError(f"{what} has an encoded-word that can't be decoded") from None
    decoded = check_header(decoded, what)
    if _ENCODED_WORD.search(decoded):
        raise ValueError(f"{what} is encoded more than once")
    return decoded


def _display_name(name: str, what: str) -> str:
    """A display name, decoded and checked (:func:`decode_words`). Display
    names are set from :class:`Address` objects, which skip the line-break
    check the SMTP policy applies to plain headers."""
    return decode_words(name, f"{what} display name")


def msg_ids(value: str, what: str) -> list[str]:
    """The msg-ids (``<id@domain>``) in a Message-ID, In-Reply-To or
    References value from another mailer, in order and without repeats.
    ValueError when the value is unsafe (:func:`decode_words`); anything
    else that is not a msg-id (a comment, a malformed id) is left out."""
    return list(dict.fromkeys(_MSG_ID.findall(decode_words(value, what))))


def check_msg_ids(value: str, what: str, *, single: bool = False) -> str:
    """``value`` for In-Reply-To (``single``: one msg-id) or References
    (msg-ids separated by spaces), after :func:`decode_words`. ValueError for
    anything else."""
    ids = decode_words(value, what).split()
    if not ids or not all(_MSG_ID.fullmatch(i) for i in ids) or (single and len(ids) != 1):
        raise ValueError(f"{what} must be {'one msg-id' if single else 'msg-ids'} like <id@example.com>")
    return " ".join(ids)


def parse_addresses(values: Iterable[str], what: str) -> list[Address]:
    """Each value is one or more addresses (``a@x.co``, ``Ann <a@x.co>``,
    ``a@x.co, b@y.co``); ValueError for anything that is not an address.
    Encoded-word display names are decoded and checked like any header."""
    out: list[Address] = []
    for value in values:
        text = check_header(str(value), what).strip()
        if not text:
            continue
        for name, addr in email.utils.getaddresses([text]):
            if not _ADDR_SPEC.match(addr):
                raise ValueError(f"{what} has something that is not an email address")
            local, _, domain = addr.rpartition("@")
            out.append(Address(display_name=_display_name(name, what), username=local, domain=domain))
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
    """An RFC 2822 message (CRLF line endings) ready for Gmail's ``raw``.

    ValueError for an unsafe header: Subject goes through
    :func:`decode_words`, In-Reply-To and References through
    :func:`check_msg_ids`, and every header is checked again as the standard
    library parsed it."""
    msg = EmailMessage(policy=email.policy.SMTP)
    if sender is not None:
        msg["From"] = sender
    for name, addresses in (("To", tuple(to)), ("Cc", tuple(cc)), ("Bcc", tuple(bcc))):
        if addresses:
            msg[name] = addresses
    msg["Subject"] = decode_words(subject, "subject")
    if in_reply_to:
        msg["In-Reply-To"] = check_msg_ids(in_reply_to, "In-Reply-To", single=True)
    if references:
        msg["References"] = check_msg_ids(references, "References")
    # What the standard library parsed is what its generator writes, so check
    # that too: it doesn't rest on decode_header agreeing with the parser.
    for name in _BUILT_HEADERS:
        if name in msg:
            check_header(str(msg[name]), name)
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
