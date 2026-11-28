"""Send an existing draft, once (docs/design.md, Interactive: send).

`send_prepare(draft_id)` previews recipients, subject and a body hash. `send_apply` checks
for an open send and journals `in_flight` under one lock, durably, before the call, so
neither a concurrent apply nor a lost answer, a timeout or a restart can lead to a second
send: while a send of a draft is `in_flight` or `uncertain`, prepare, apply and
`draft_put` on it are refused until `send_reconcile` settles it.

Reconcile uses what Gmail does on send: the draft disappears and a sent message appears in
its thread. A draft that is still there was not sent. A draft that is gone, with a new
sent message in its thread carrying the same recipients, subject and body, was sent.
Anything else stays `uncertain` for the owner. The journal keeps only hashes of these,
never the addresses or the subject.
"""

import email.utils
import hashlib
import threading
from collections.abc import Callable, Iterable
from typing import Any

from hermes_gmail import bounds
from hermes_gmail.errors import Category, GateError
from hermes_gmail.health import Health
from hermes_gmail.interactive import WRITE_DEADLINE_SECONDS, Interactive, quote
from hermes_gmail.journal import Journal
from hermes_gmail.mime import TEXT_CAP_MAX, extract_text
from hermes_gmail.reads import Reads
from hermes_gmail.transport import Transport

_OPEN = frozenset({"in_flight", "uncertain"})
_ADDRESS_HEADERS = ("to", "cc", "bcc")
_COMPARED = ("to", "cc", "bcc", "subject", "body_hash")
_SHOWN_RECIPIENTS = 20


class Sender:
    def __init__(
        self,
        *,
        transport: Transport,
        reads: Reads,
        interactive: Interactive,
        journal: Callable[[], Journal],
        health: Health,
        deadline: Callable[[float], float],
    ) -> None:
        self._transport = transport
        self._reads = reads
        self._interactive = interactive
        self._journal = journal
        self._health = health
        self._deadline = deadline
        self._lock = threading.Lock()
        self._live: set[str] = set()  # drafts whose send call is running in this process
        interactive.draft_guard = self.guard_draft

    def guard_draft(self, draft_id: str) -> None:
        """Refuse touching a draft while a send of it is running, in flight or uncertain."""
        with self._lock:
            if draft_id in self._live or self._open_send(draft_id) is not None:
                raise bounds.refused("a send of this draft is in flight or uncertain; reconcile it first")

    def send_prepare(self, draft_id: Any, caller: str = "local") -> dict[str, Any]:
        did = bounds.gmail_id(draft_id, "draft_id")
        self.guard_draft(did)
        draft = self._draft(did)
        if draft is None:
            raise GateError(Category.GMAIL_NOT_FOUND, "Gmail has no such draft")
        if not any(_addresses(draft[name]) for name in _ADDRESS_HEADERS):
            raise bounds.refused("the draft has no recipients")
        thread = self._reads.read_thread(draft["thread_id"], bounds.THREAD_BOUND_MAX, 1)
        earlier = [m["id"] for m in thread["messages"] if m["direction"] != "draft"]
        summary = f"Send this draft: {_recipients(draft)}; subject {quote(draft['subject'])}."
        return self._interactive.store_preview("send", caller, {**draft, "earlier": earlier}, summary)

    def send_apply(self, preview_id: Any, approval: Any = None, caller: str = "local") -> dict[str, Any]:
        preview = self._interactive.take_preview("send", preview_id, approval, caller)
        expected = preview.payload
        did = expected["draft_id"]
        with self._lock:
            if did in self._live or self._open_send(did) is not None:
                self._interactive.previews.move(preview.id, from_status="applying", to_status="stale")
                raise bounds.refused("a send of this draft is in flight or uncertain; reconcile it first")
            current = self._draft(did)
            if current is None or any(current[key] != expected[key] for key in _COMPARED):
                self._interactive.previews.move(preview.id, from_status="applying", to_status="stale")
                raise bounds.refused("the draft changed or disappeared since the preview; prepare again")
            # Durable before the call: after a crash this row is what stops a second send.
            entry = self._journal().record(
                caller=caller,
                operation="send_apply",
                status="in_flight",
                preview_hash=preview.hash,
                approval=approval,
                gmail_ids=[did, expected["thread_id"]],
                fingerprint=_fingerprint(expected),
            )
            self._live.add(did)
        self._interactive.previews.move(preview.id, from_status="applying", to_status="used")
        try:
            return self._send(did, entry, expected["thread_id"])
        finally:
            with self._lock:
                self._live.discard(did)

    def send_reconcile(self, draft_id: Any, caller: str = "local") -> dict[str, Any]:
        did = bounds.gmail_id(draft_id, "draft_id")
        with self._lock:
            if did in self._live:
                raise bounds.refused("the send of this draft is still running; reconcile after it ends")
            entry = self._open_send(did)
        if entry is None:
            raise bounds.refused("no send of this draft is waiting to be reconciled")
        if self._draft(did) is not None:
            self._journal().update(entry["id"], status="not_sent")
            return {"status": "not_sent"}
        match = self._sent_match(entry["fingerprint"]) if entry["fingerprint"] else None
        if match is not None:
            self._journal().update(entry["id"], status="sent", gmail_ids=[*entry["gmail_ids"], match])
            return {"status": "sent", "message_id": match}
        self._journal().update(entry["id"], status="uncertain")
        return {"status": "uncertain"}

    def _send(self, draft_id: str, entry: int, thread_id: str) -> dict[str, Any]:
        deadline = self._deadline(WRITE_DEADLINE_SECONDS)
        try:
            reply = self._transport.write("POST", "drafts/send", {"id": draft_id}, deadline=deadline)
        except GateError as error:
            if error.category is Category.UNCERTAIN:
                self._journal().update(entry, status="uncertain")
                raise self._health.fail(
                    Category.UNCERTAIN, "the send may or may not have happened; run send_reconcile, never send again"
                ) from None
            self._journal().update(entry, status="failed")
            raise
        sent_id = reply.get("id")
        if not isinstance(sent_id, str):
            self._journal().update(entry, status="uncertain")
            raise self._health.fail(Category.UNCERTAIN, "Gmail did not say which message it sent; run send_reconcile")
        self._journal().update(entry, status="sent", gmail_ids=[draft_id, thread_id, sent_id])
        try:
            labels = self._transport.read(f"messages/{sent_id}", {"format": "minimal"}, deadline=deadline).get(
                "labelIds", []
            )
        except GateError:
            labels = []  # sent and journaled; only the read-back failed
        verified = isinstance(labels, list) and "SENT" in labels
        if verified:
            self._health.success("interactive")
        return {"status": "sent", "message_id": sent_id, "verified": verified}

    def _open_send(self, draft_id: str) -> dict[str, Any] | None:
        for entry in reversed(self._journal().entries("send_apply")):
            if draft_id in entry["gmail_ids"] and entry["status"] in _OPEN:
                return entry
        return None

    def _draft(self, draft_id: str) -> dict[str, Any] | None:
        try:
            stored = self._transport.read(
                f"drafts/{draft_id}", {"format": "full"}, deadline=self._deadline(WRITE_DEADLINE_SECONDS)
            )
        except GateError as error:
            if error.category is Category.GMAIL_NOT_FOUND:
                return None
            raise
        message = stored.get("message") if isinstance(stored.get("message"), dict) else {}
        if not isinstance(message.get("threadId"), str):
            raise GateError(Category.GMAIL_FAILED, "Gmail's draft reply has an unexpected shape")
        return {"draft_id": draft_id, "thread_id": message["threadId"], **_identity(message)}

    def _sent_match(self, fingerprint: dict[str, Any]) -> str | None:
        try:
            thread = self._reads.read_thread(fingerprint["thread_id"], bounds.THREAD_BOUND_MAX, TEXT_CAP_MAX)
        except GateError as error:
            if error.category is Category.GMAIL_NOT_FOUND:
                return None
            raise
        earlier = set(fingerprint["earlier"])
        for message in thread["messages"]:
            if message["id"] in earlier or message["direction"] != "sent":
                continue
            headers = {h["name"].lower(): h["value"] for h in message["headers"]}
            seen = {
                "to_cc": _digest(_addresses(headers.get("to", "")) | _addresses(headers.get("cc", ""))),
                "subject": _digest([headers.get("subject", "")]),
                "body": _body_hash(message.get("text", "")),
            }
            same = all(seen[key] == fingerprint[key] for key in seen)
            # Bcc and Message-ID count when both sides have them; Gmail may not echo them.
            if "bcc" in headers and fingerprint["bcc"] is not None:
                same = same and _digest(_addresses(headers["bcc"])) == fingerprint["bcc"]
            if fingerprint["message_id"] is not None and "message-id" in headers:
                same = same and _digest([headers["message-id"]]) == fingerprint["message_id"]
            if same:
                return message["id"]
        return None


def _identity(message: dict[str, Any]) -> dict[str, Any]:
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    headers: dict[str, str] = {}
    for header in payload.get("headers", []) if isinstance(payload.get("headers"), list) else []:
        if isinstance(header, dict) and isinstance(header.get("name"), str):
            headers.setdefault(header["name"].lower(), str(header.get("value", "")))
    text = extract_text(payload, TEXT_CAP_MAX)
    return {
        "to": headers.get("to", ""),
        "cc": headers.get("cc", ""),
        "bcc": headers.get("bcc", ""),
        "subject": headers.get("subject", ""),
        "message_id_header": headers.get("message-id"),
        "body_hash": _body_hash(text.text),
    }


def _fingerprint(draft: dict[str, Any]) -> dict[str, Any]:
    """What reconcile compares, as hashes and Gmail IDs only."""
    bcc = _addresses(draft["bcc"])
    return {
        "thread_id": draft["thread_id"],
        "earlier": draft["earlier"],
        "to_cc": _digest(_addresses(draft["to"]) | _addresses(draft["cc"])),
        "bcc": _digest(bcc) if bcc else None,
        "subject": _digest([draft["subject"]]),
        "body": draft["body_hash"],
        "message_id": _digest([draft["message_id_header"]]) if draft["message_id_header"] else None,
    }


def _recipients(draft: dict[str, Any]) -> str:
    """Addresses only, never display names: a display name can look like another address."""
    parts = []
    for name in _ADDRESS_HEADERS:
        addresses = sorted(_addresses(draft[name]))
        if not addresses:
            continue
        shown = ", ".join(quote(address) for address in addresses[:_SHOWN_RECIPIENTS])
        more = len(addresses) - _SHOWN_RECIPIENTS
        parts.append(f"{name} {shown}" + (f" and {more} more" if more > 0 else ""))
    return "; ".join(parts)


def _addresses(value: str) -> frozenset[str]:
    return frozenset(address.lower() for _, address in email.utils.getaddresses([value]) if address)


def _digest(values: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(sorted(values)).encode()).hexdigest()


def _body_hash(text: str) -> str:
    return hashlib.sha256(text.rstrip("\n").encode()).hexdigest()
