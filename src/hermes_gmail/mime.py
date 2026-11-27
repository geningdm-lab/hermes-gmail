"""Plain text from a Gmail message payload, without fetching anything.

The text/plain part wins; an HTML-only message is reduced to its visible text. A part
that cannot be decoded, or a body Gmail keeps out of line, is reported with
`decode_failed` instead of returning empty or guessed text. Attachments are never read.
"""

import base64
import binascii
import re
from dataclasses import dataclass
from email.message import Message
from html.parser import HTMLParser
from typing import Any

TEXT_CAP_DEFAULT = 20_000
TEXT_CAP_MAX = 200_000
_MAX_DEPTH = 30
_SKIPPED = frozenset({"script", "style", "head", "title"})
_BLOCKS = frozenset({"p", "div", "tr", "li", "br", "hr", "table", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"})


@dataclass(frozen=True)
class Text:
    text: str
    source: str  # "plain", "html" or "none"
    truncated: bool
    decode_failed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "text_source": self.source,
            "truncated": self.truncated,
            "decode_failed": self.decode_failed,
        }


def extract_text(payload: dict[str, Any], cap: int = TEXT_CAP_DEFAULT) -> Text:
    plain, html = _find(payload, 0)
    if plain is not None:
        text, failed = _decode(plain)
        source = "plain"
    elif html is not None:
        markup, failed = _decode(html)
        text = _visible_text(markup)
        source = "html"
    else:
        text, failed, source = "", False, "none"
    truncated = len(text) > cap
    return Text(text[:cap], source, truncated, failed)


def _find(part: Any, depth: int) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The first text/plain and the first text/html part, skipping attachments."""
    if not isinstance(part, dict) or depth > _MAX_DEPTH or part.get("filename"):
        return None, None
    mime_type = str(part.get("mimeType", "")).lower()
    if mime_type.startswith("multipart/"):
        plain = html = None
        children = part.get("parts")
        for child in children if isinstance(children, list) else []:
            found_plain, found_html = _find(child, depth + 1)
            plain = plain or found_plain
            html = html or found_html
        return plain, html
    if mime_type == "text/plain":
        return part, None
    if mime_type == "text/html":
        return None, part
    return None, None


def _decode(part: dict[str, Any]) -> tuple[str, bool]:
    body = part.get("body") if isinstance(part.get("body"), dict) else {}
    data = body.get("data")
    if not isinstance(data, str) or not data:
        # Out of line (attachmentId) or missing: reading it would be another request.
        return "", bool(body.get("attachmentId") or body.get("size"))
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except binascii.Error, ValueError:
        return "", True
    charset = _charset(part) or "utf-8"
    try:
        return raw.decode(charset), False
    except LookupError, UnicodeDecodeError:
        return raw.decode("utf-8", "replace"), True


def _charset(part: dict[str, Any]) -> str | None:
    headers = part.get("headers")
    for header in headers if isinstance(headers, list) else []:
        if isinstance(header, dict) and str(header.get("name", "")).lower() == "content-type":
            message = Message()
            message["Content-Type"] = str(header.get("value", ""))
            return message.get_content_charset()
    return None


class _Visible(HTMLParser):
    """Collects visible text. HTMLParser only parses the string it is given: no image,
    stylesheet or link is ever fetched."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED:
            self._skip += 1
        elif tag in _BLOCKS:
            self.chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCKS:
            self.chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.chunks.append(data)


def _visible_text(markup: str) -> str:
    parser = _Visible()
    parser.feed(markup)
    parser.close()
    lines = (re.sub(r"[ \t\r\f\v\xa0]+", " ", line).strip() for line in "".join(parser.chunks).splitlines())
    return "\n".join(line for line in lines if line)
