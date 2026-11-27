"""Draft MIME: built by the gate, so threading headers are always right, and compared back
after Gmail stores it."""

import base64
import email.utils
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

from hermes_gmail import bounds
from hermes_gmail.mime import TEXT_CAP_MAX, extract_text

RECIPIENTS_MAX = 50
SUBJECT_MAX = 998
BODY_MAX = 200_000


@dataclass(frozen=True)
class Draft:
    to: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    body: str
    in_reply_to: str | None = None
    references: str | None = None

    @classmethod
    def parse(cls, to: Any, cc: Any, subject: Any, body: Any) -> Draft:
        recipients_to = _addresses(to, "to", required=True)
        recipients_cc = _addresses(cc if cc is not None else [], "cc", required=False)
        if len(recipients_to) + len(recipients_cc) > RECIPIENTS_MAX:
            raise bounds.refused(f"a draft may have at most {RECIPIENTS_MAX} recipients")
        if not isinstance(subject, str) or len(subject) > SUBJECT_MAX or _has_line_break(subject):
            raise bounds.refused(f"subject must be one line of at most {SUBJECT_MAX} characters")
        if not isinstance(body, str) or len(body) > BODY_MAX:
            raise bounds.refused(f"body must be text of at most {BODY_MAX} characters")
        return cls(recipients_to, recipients_cc, subject, body)

    def replying_to(self, message_id: str | None, references: str | None) -> Draft:
        if not message_id:
            return self
        chain = " ".join(part for part in (references, message_id) if part)
        return Draft(self.to, self.cc, self.subject, self.body, message_id, chain)

    def raw(self) -> str:
        message = EmailMessage()
        message["To"] = ", ".join(self.to)
        if self.cc:
            message["Cc"] = ", ".join(self.cc)
        message["Subject"] = self.subject
        if self.in_reply_to:
            message["In-Reply-To"] = self.in_reply_to
            message["References"] = self.references or self.in_reply_to
        message.set_content(self.body)
        return base64.urlsafe_b64encode(message.as_bytes()).decode()

    def matches(self, stored: dict[str, Any]) -> bool:
        """Gmail's stored draft says the same: recipients, subject, threading and body."""
        payload = stored.get("payload") if isinstance(stored.get("payload"), dict) else {}
        headers: dict[str, str] = {}
        for header in payload.get("headers", []) if isinstance(payload.get("headers"), list) else []:
            if isinstance(header, dict) and isinstance(header.get("name"), str):
                headers.setdefault(header["name"].lower(), str(header.get("value", "")))
        text = extract_text(payload, TEXT_CAP_MAX)
        return (
            _normalized(headers.get("to", "")) == _normalized(", ".join(self.to))
            and _normalized(headers.get("cc", "")) == _normalized(", ".join(self.cc))
            and headers.get("subject", "") == self.subject
            and headers.get("in-reply-to") == self.in_reply_to
            and not text.decode_failed
            and text.text.rstrip("\n") == self.body.rstrip("\n").replace("\r\n", "\n")
        )


def _addresses(value: Any, what: str, *, required: bool) -> tuple[str, ...]:
    if not isinstance(value, list) or (required and not value):
        raise bounds.refused(f"{what} must be a {'non-empty ' if required else ''}list of addresses")
    result = []
    for item in value:
        if not isinstance(item, str) or _has_line_break(item) or len(item) > 320:
            raise bounds.refused(f"{what} has a malformed address")
        parsed = email.utils.getaddresses([item])
        if len(parsed) != 1 or "@" not in parsed[0][1]:
            raise bounds.refused(f"{what} has a malformed address")
        result.append(item.strip())
    return tuple(result)


def _normalized(value: str) -> frozenset[str]:
    return frozenset(address.lower() for _, address in email.utils.getaddresses([value]) if address)


def _has_line_break(value: str) -> bool:
    return "\r" in value or "\n" in value
