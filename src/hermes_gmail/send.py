"""Send an existing draft, once (docs/design.md, Interactive: send).

`send_prepare(draft_id)` stores a preview of recipients, subject and a body hash.
`send_apply` journals `in_flight` durably before the call, so a lost answer, a timeout or a
restart can never lead to a second send: while a send of a draft is `in_flight` or
`uncertain`, another one is refused until `send_reconcile` settles it.

Reconcile uses what Gmail does on send: the draft disappears and a sent message appears in
its thread. A draft that is still there was not sent. A draft that is gone, with a new
sent message in its thread carrying the same recipients, subject and body, was sent.
Anything else stays `uncertain` for the owner to look at.
"""

import hashlib
from collections.abc import Callable
from typing import Any

from hermes_gmail import bounds
from hermes_gmail.errors import Category, GateError
from hermes_gmail.health import Health
from hermes_gmail.interactive import WRITE_DEADLINE_SECONDS, Interactive
from hermes_gmail.journal import Journal
from hermes_gmail.mime import TEXT_CAP_MAX, extract_text
from hermes_gmail.reads import Reads
from hermes_gmail.transport import Transport

_OPEN = frozenset({"in_flight", "uncertain"})
_ADDRESS_HEADERS = ("to", "cc", "bcc")


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

    def send_prepare(self, draft_id: Any, caller: str = "local") -> dict[str, Any]:
        did = bounds.gmail_id(draft_id, "draft_id")
        self._refuse_if_open(did)
        draft = self._draft(did)
        if draft is None:
            raise GateError(Category.GMAIL_NOT_FOUND, "Gmail has no such draft")
        if not draft["to"] and not draft["cc"] and not draft["bcc"]:
            raise bounds.refused("the draft has no recipients")
        thread = self._reads.read_thread(draft["thread_id"], bounds.THREAD_BOUND_MAX, 1)
        payload = {**draft, "thread_message_ids": [m["id"] for m in thread["messages"]]}
        recipients = "; ".join(f"{name} {draft[name]}" for name in _ADDRESS_HEADERS if draft[name])
        summary = f'Send the draft "{draft["subject"]}": {recipients}.'
        return self._interactive.store_preview("send", caller, payload, summary)

    def send_apply(self, preview_id: Any, approval: Any = None, caller: str = "local") -> dict[str, Any]:
        preview = self._interactive.take_preview("send", preview_id, approval, caller)
        expected = preview.payload
        did = expected["draft_id"]
        if self._open_send(did) is not None:
            self._journal().move_preview(preview.id, from_status="applying", to_status="stale")
            raise bounds.refused("a send of this draft is in flight or uncertain; reconcile it first")
        current = self._draft(did)
        if current is None or any(current[key] != expected[key] for key in _COMPARED):
            self._journal().move_preview(preview.id, from_status="applying", to_status="stale")
            raise bounds.refused("the draft changed or disappeared since the preview; prepare again")
        # Durable before the call: after a crash this row is what stops a second send.
        entry = self._journal().record(
            caller=caller,
            operation="send_apply",
            status="in_flight",
            preview_hash=preview.hash,
            approval=approval,
            gmail_ids=[did, expected["thread_id"]],
        )
        self._journal().move_preview(preview.id, from_status="applying", to_status="used")
        try:
            reply = self._transport.write(
                "POST", "drafts/send", {"id": did}, deadline=self._deadline(WRITE_DEADLINE_SECONDS)
            )
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
        self._journal().update(entry, status="sent", gmail_ids=[did, expected["thread_id"], sent_id])
        labels = self._transport.read(
            f"messages/{sent_id}", {"format": "minimal"}, deadline=self._deadline(WRITE_DEADLINE_SECONDS)
        ).get("labelIds", [])
        verified = isinstance(labels, list) and "SENT" in labels
        if verified:
            self._health.success("interactive")
        return {"status": "sent", "message_id": sent_id, "verified": verified}

    def send_reconcile(self, draft_id: Any, caller: str = "local") -> dict[str, Any]:
        did = bounds.gmail_id(draft_id, "draft_id")
        entry = self._open_send(did)
        if entry is None:
            raise bounds.refused("no send of this draft is waiting to be reconciled")
        preview = self._preview_for(entry)
        if self._draft(did) is not None:
            self._journal().update(entry["id"], status="not_sent")
            return {"status": "not_sent"}
        if preview is not None:
            match = self._sent_match(preview)
            if match is not None:
                self._journal().update(entry["id"], status="sent", gmail_ids=[*entry["gmail_ids"], match])
                return {"status": "sent", "message_id": match}
        self._journal().update(entry["id"], status="uncertain")
        return {"status": "uncertain"}

    def _open_send(self, draft_id: str) -> dict[str, Any] | None:
        for entry in reversed(self._journal().entries("send_apply")):
            if draft_id in entry["gmail_ids"] and entry["status"] in _OPEN:
                return entry
        return None

    def _refuse_if_open(self, draft_id: str) -> None:
        if self._open_send(draft_id) is not None:
            raise bounds.refused("a send of this draft is in flight or uncertain; reconcile it first")

    def _preview_for(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        found = self._journal().preview_by_hash(entry["preview_hash"]) if entry["preview_hash"] else None
        return found.payload if found is not None else None

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

    def _sent_match(self, preview: dict[str, Any]) -> str | None:
        try:
            thread = self._reads.read_thread(preview["thread_id"], bounds.THREAD_BOUND_MAX, TEXT_CAP_MAX)
        except GateError as error:
            if error.category is Category.GMAIL_NOT_FOUND:
                return None
            raise
        earlier = set(preview["thread_message_ids"])
        for message in thread["messages"]:
            if message["id"] in earlier or "SENT" not in message["label_ids"]:
                continue
            headers = {h["name"].lower(): h["value"] for h in message["headers"]}
            same = (
                headers.get("subject", "") == preview["subject"]
                and _hash(message.get("text", "")) == preview["body_hash"]
                and _addresses(headers.get("to", "")) == _addresses(preview["to"])
                and (not preview["message_id_header"] or headers.get("message-id") == preview["message_id_header"])
            )
            if same:
                return message["id"]
        return None


_COMPARED = ("to", "cc", "bcc", "subject", "body_hash")


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
        "body_hash": _hash(text.text),
    }


def _hash(text: str) -> str:
    return hashlib.sha256(text.rstrip("\n").encode()).hexdigest()


def _addresses(value: str) -> frozenset[str]:
    import email.utils

    return frozenset(address.lower() for _, address in email.utils.getaddresses([value]) if address)
