"""Interactive writes (docs/design.md, Operations: Interactive).

Label changes and filters are two steps. `*_prepare` reads the mailbox and stores an
immutable preview: targets, changes, the state they were computed from, a hash and an
expiry. `*_apply(preview_id, approval)` takes the preview once, re-reads the targets,
stops with `refused` if anything changed, writes, and reads back.

`label_create` and `draft_put` are one step: a label is idempotent by name and
authorizes nothing, and a draft is idempotent per thread and changes nothing anyone else
sees. Hermes' trust prompt is their approval.
"""

import hashlib
import json
import secrets
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from hermes_gmail import bounds
from hermes_gmail.compose import Draft
from hermes_gmail.errors import Category, GateError
from hermes_gmail.health import Health
from hermes_gmail.journal import Journal, Preview
from hermes_gmail.reads import Reads
from hermes_gmail.transport import Transport

PREVIEW_TTL_SECONDS = 900.0
WRITE_DEADLINE_SECONDS = 300.0
MODIFY_TARGETS_MAX = 1000
APPROVALS = frozenset({"elicitation"})
MODIFIABLE_SYSTEM_LABELS = frozenset({"INBOX", "UNREAD", "STARRED", "IMPORTANT"})
UNTOUCHABLE = frozenset({"SPAM", "TRASH"})
FILTER_CRITERIA = frozenset(
    {"from", "to", "subject", "query", "negatedQuery", "hasAttachment", "excludeChats", "size", "sizeComparison"}
)
FILTER_ADD_SYSTEM = frozenset({"STARRED", "IMPORTANT"})
FILTER_REMOVE_SYSTEM = frozenset({"INBOX", "UNREAD", "IMPORTANT", "SPAM"})
_LABEL_NAME_MAX = 225


class Interactive:
    def __init__(
        self,
        *,
        transport: Transport,
        reads: Reads,
        journal: Callable[[], Journal],
        health: Health,
        parallel: int,
        deadline: Callable[[float], float],
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._transport = transport
        self._reads = reads
        self._journal = journal
        self._health = health
        self._parallel = parallel
        self._deadline = deadline
        self._wall = wall_clock

    # Labels

    def label_create(self, name: Any, caller: str = "local") -> dict[str, Any]:
        label_name = _label_name(name)
        existing = self._label_by_name(label_name)
        if existing is not None:
            return {"label": existing, "created": False}
        entry = self._journal().record(caller=caller, operation="label_create", status="in_flight")
        try:
            self._transport.write(
                "POST",
                "labels",
                {"name": label_name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
                deadline=self._deadline(WRITE_DEADLINE_SECONDS),
            )
        except GateError as error:
            if error.category is not Category.UNCERTAIN:
                self._journal().update(entry, status="failed")
                raise
        label = self._label_by_name(label_name)  # idempotent by name: the list is the truth
        if label is None:
            self._journal().update(entry, status="uncertain")
            raise self._health.fail(Category.UNCERTAIN, "the label may or may not have been created")
        self._journal().update(entry, status="done", gmail_ids=[label["id"]])
        self._health.success("interactive")
        return {"label": label, "created": True}

    # Label changes on messages

    def modify_prepare(
        self,
        ids: Any = None,
        query: Any = None,
        add: Any = None,
        remove: Any = None,
        caller: str = "local",
    ) -> dict[str, Any]:
        if (ids is None) == (query is None):
            raise bounds.refused("give either ids or query")
        if ids is not None:
            targets = bounds.gmail_ids(ids, maximum=MODIFY_TARGETS_MAX)
        else:
            found = self._reads.search(query, 2)
            if not found["complete"] or len(found["ids"]) > MODIFY_TARGETS_MAX:
                raise bounds.refused(f"the query matches more than {MODIFY_TARGETS_MAX} messages; narrow it")
            targets = found["ids"]
            if not targets:
                raise bounds.refused("the query matches no messages")
        add_names, remove_names = _names(add, "add"), _names(remove, "remove")
        if not add_names and not remove_names:
            raise bounds.refused("nothing to add or remove")
        if set(add_names) & set(remove_names):
            raise bounds.refused("a label cannot be both added and removed")
        add_ids = self._resolve(add_names)
        remove_ids = self._resolve(remove_names)
        deadline = self._deadline(WRITE_DEADLINE_SECONDS)
        states = self._states(targets, deadline)
        missing = [t for t, s in zip(targets, states, strict=True) if s is None]
        if missing:
            raise bounds.refused(f"{len(missing)} of the messages do not exist")
        if any(s & UNTOUCHABLE for s in states if s is not None):
            raise bounds.refused("the targets include spam or trash")
        payload = {
            "ids": targets,
            "add": add_names,
            "remove": remove_names,
            "add_ids": add_ids,
            "remove_ids": remove_ids,
            "before": {t: sorted(s) for t, s in zip(targets, states, strict=True) if s is not None},
        }
        changes = "; ".join(
            part
            for part in (
                f"add {', '.join(add_names)}" if add_names else "",
                f"remove {', '.join(remove_names)}" if remove_names else "",
            )
            if part
        )
        summary = f"Change labels on {len(targets)} message(s): {changes}."
        return self.store_preview("modify", caller, payload, summary)

    def modify_apply(self, preview_id: Any, approval: Any = None, caller: str = "local") -> dict[str, Any]:
        preview = self.take_preview("modify", preview_id, approval, caller)
        payload = preview.payload
        deadline = self._deadline(WRITE_DEADLINE_SECONDS)
        current = self._states(payload["ids"], deadline)
        before = {t: frozenset(s) for t, s in payload["before"].items()}
        if any(state != before[t] for t, state in zip(payload["ids"], current, strict=True)):
            self._journal().move_preview(preview.id, from_status="applying", to_status="stale")
            raise bounds.refused("the messages changed since the preview; prepare again")
        add, remove = set(payload["add_ids"]), set(payload["remove_ids"])
        desired = {t: (before[t] | add) - remove for t in payload["ids"]}
        changing = [t for t in payload["ids"] if desired[t] != before[t]]
        entry = self._journal().record(
            caller=caller,
            operation="modify_apply",
            status="in_flight",
            preview_hash=preview.hash,
            approval=approval,
            gmail_ids=changing,
        )
        stopped = None
        if changing:
            try:
                self._transport.write(
                    "POST",
                    "messages/batchModify",
                    {"ids": changing, "addLabelIds": sorted(add), "removeLabelIds": sorted(remove)},
                    deadline=deadline,
                )
            except GateError as error:
                stopped = error.category
        outcome: dict[str, list[str]] = {"verified": [], "mismatched": [], "unverified": []}
        for target, state in zip(changing, self._states(changing, deadline, tolerate=True), strict=True):
            key = "unverified" if state is None else "verified" if state == desired[target] else "mismatched"
            outcome[key].append(target)
        clean = stopped is None and not outcome["mismatched"] and not outcome["unverified"]
        self._journal().update(entry, status="done" if clean else "partial")
        self._journal().move_preview(preview.id, from_status="applying", to_status="used")
        if clean:
            self._health.success("interactive")
        unchanged = [t for t in payload["ids"] if t not in set(changing)]
        return {"stopped": stopped and str(stopped), "unchanged": unchanged, **outcome}

    # Filters

    def filter_prepare(
        self, criteria: Any, add: Any = None, remove: Any = None, caller: str = "local"
    ) -> dict[str, Any]:
        clean_criteria = _criteria(criteria)
        add_names, remove_names = _names(add, "add"), _names(remove, "remove")
        if not add_names and not remove_names:
            raise bounds.refused("a filter needs an action")
        labels = self._labels()
        user_adds = [n for n in add_names if n not in FILTER_ADD_SYSTEM]
        if len(user_adds) > 1:
            raise bounds.refused("Gmail allows at most one user label per filter")
        if set(remove_names) - FILTER_REMOVE_SYSTEM:
            raise bounds.refused(f"a filter may only remove {', '.join(sorted(FILTER_REMOVE_SYSTEM))}")
        add_ids = []
        for name in add_names:
            if name in FILTER_ADD_SYSTEM:
                add_ids.append(name)
                continue
            label = labels.get(name)
            if label is None or label["type"] != "user":
                raise bounds.refused("a filter may only add an existing user label, STARRED or IMPORTANT")
            add_ids.append(label["id"])
        action = {"addLabelIds": sorted(add_ids), "removeLabelIds": sorted(remove_names)}
        if any(_same_filter(f, clean_criteria, action) for f in self._reads.filters()["filters"]):
            raise bounds.refused("an identical filter already exists")
        summary = f"Create a filter for {_describe(clean_criteria)}: " + "; ".join(
            part
            for part in (
                f"add {', '.join(add_names)}" if add_names else "",
                f"remove {', '.join(remove_names)}" if remove_names else "",
            )
            if part
        )
        return self.store_preview("filter", caller, {"criteria": clean_criteria, "action": action}, summary + ".")

    def filter_apply(self, preview_id: Any, approval: Any = None, caller: str = "local") -> dict[str, Any]:
        preview = self.take_preview("filter", preview_id, approval, caller)
        criteria, action = preview.payload["criteria"], preview.payload["action"]
        if any(_same_filter(f, criteria, action) for f in self._reads.filters()["filters"]):
            self._journal().move_preview(preview.id, from_status="applying", to_status="stale")
            raise bounds.refused("an identical filter appeared since the preview")
        entry = self._journal().record(
            caller=caller, operation="filter_apply", status="in_flight", preview_hash=preview.hash, approval=approval
        )
        created_id = None
        try:
            reply = self._transport.write(
                "POST",
                "settings/filters",
                {"criteria": criteria, "action": action},
                deadline=self._deadline(WRITE_DEADLINE_SECONDS),
            )
            created_id = reply.get("id") if isinstance(reply.get("id"), str) else None
        except GateError as error:
            if error.category is not Category.UNCERTAIN:
                self._journal().update(entry, status="failed")
                self._journal().move_preview(preview.id, from_status="applying", to_status="open")
                raise
        # Read back; after an uncertain create, this is the reconcile: look for it.
        found = [f for f in self._reads.filters()["filters"] if _same_filter(f, criteria, action)]
        if created_id is not None:
            found = [f for f in found if f["id"] == created_id]
        self._journal().move_preview(preview.id, from_status="applying", to_status="used")
        if not found:
            self._journal().update(entry, status="uncertain")
            raise self._health.fail(Category.UNCERTAIN, "the filter may or may not have been created")
        self._journal().update(entry, status="done", gmail_ids=[found[0]["id"]])
        self._health.success("interactive")
        return {"filter": found[0], "verified": True}

    def filter_delete_prepare(self, filter_id: Any, caller: str = "local") -> dict[str, Any]:
        fid = bounds.gmail_id(filter_id, "filter_id")
        current = self._filter(fid)
        if current is None:
            raise GateError(Category.GMAIL_NOT_FOUND, "Gmail has no such filter")
        summary = f"Delete the filter for {_describe(current['criteria'])}."
        return self.store_preview("filter_delete", caller, {"filter": current}, summary)

    def filter_delete_apply(self, preview_id: Any, approval: Any = None, caller: str = "local") -> dict[str, Any]:
        preview = self.take_preview("filter_delete", preview_id, approval, caller)
        expected = preview.payload["filter"]
        current = self._filter(expected["id"])
        if current is None or not _same_filter(current, expected["criteria"], expected["action"]):
            self._journal().move_preview(preview.id, from_status="applying", to_status="stale")
            raise bounds.refused("the filter changed or disappeared since the preview")
        entry = self._journal().record(
            caller=caller,
            operation="filter_delete_apply",
            status="in_flight",
            preview_hash=preview.hash,
            approval=approval,
            gmail_ids=[expected["id"]],
        )
        try:
            self._transport.write(
                "DELETE", f"settings/filters/{expected['id']}", {}, deadline=self._deadline(WRITE_DEADLINE_SECONDS)
            )
        except GateError as error:
            if error.category not in (Category.UNCERTAIN, Category.GMAIL_NOT_FOUND):
                self._journal().update(entry, status="failed")
                self._journal().move_preview(preview.id, from_status="applying", to_status="open")
                raise
        self._journal().move_preview(preview.id, from_status="applying", to_status="used")
        if self._filter(expected["id"]) is not None:
            self._journal().update(entry, status="uncertain")
            raise self._health.fail(Category.UNCERTAIN, "the filter is still there after the delete")
        self._journal().update(entry, status="done")
        self._health.success("interactive")
        return {"deleted": expected["id"], "verified": True}

    # Drafts

    def draft_put(
        self,
        to: Any,
        subject: Any,
        body: Any,
        cc: Any = None,
        thread_id: Any = None,
        draft_id: Any = None,
        caller: str = "local",
    ) -> dict[str, Any]:
        """Create or update the draft. In a thread, the draft already attached to it is
        updated instead of a second one being created, and the reply headers are set."""
        draft = Draft.parse(to, cc, subject, body)
        tid = None if thread_id is None else bounds.gmail_id(thread_id, "thread_id")
        did = None if draft_id is None else bounds.gmail_id(draft_id, "draft_id")
        if tid is not None:
            existing = [d for d in self._reads.drafts(tid)["drafts"]]
            if did is None and len(existing) > 1:
                raise bounds.refused("the thread has several drafts; name the one to update")
            if did is None and existing:
                did = existing[0]["id"]
            if did is not None and did not in {d["id"] for d in existing}:
                raise bounds.refused("that draft is not in this thread")
            latest = self._reads.read_thread(tid, 1, 1)["messages"]
            if latest:
                headers = {h["name"].lower(): h["value"] for h in latest[-1]["headers"]}
                draft = draft.replying_to(headers.get("message-id"), headers.get("references"))
        message: dict[str, Any] = {"raw": draft.raw()}
        if tid is not None:
            message["threadId"] = tid
        deadline = self._deadline(WRITE_DEADLINE_SECONDS)
        entry = self._journal().record(
            caller=caller,
            operation="draft_put",
            status="in_flight",
            gmail_ids=[x for x in (did, tid) if x],
        )
        try:
            if did is not None:
                reply = self._transport.write(
                    "PUT", f"drafts/{did}", {"id": did, "message": message}, deadline=deadline
                )
            else:
                reply = self._transport.write("POST", "drafts", {"message": message}, deadline=deadline)
        except GateError as error:
            self._journal().update(entry, status="uncertain" if error.category is Category.UNCERTAIN else "failed")
            if error.category is Category.UNCERTAIN:
                raise self._health.fail(
                    Category.UNCERTAIN, "the draft may or may not have been saved; read the thread's drafts first"
                ) from None
            raise
        saved_id = reply.get("id")
        if not isinstance(saved_id, str):
            self._journal().update(entry, status="uncertain")
            raise self._health.fail(Category.UNCERTAIN, "Gmail did not say which draft it saved")
        stored = self._transport.read(f"drafts/{saved_id}", {"format": "full"}, deadline=deadline)
        stored_message = stored.get("message") if isinstance(stored.get("message"), dict) else {}
        verified = draft.matches(stored_message)
        self._journal().update(entry, status="done" if verified else "mismatched", gmail_ids=[saved_id])
        if verified:
            self._health.success("interactive")
        return {
            "draft_id": saved_id,
            "message_id": stored_message.get("id"),
            "thread_id": stored_message.get("threadId"),
            "created": did is None,
            "verified": verified,
        }

    # Previews

    def preview(self, preview_id: Any, caller: str = "local") -> dict[str, Any]:
        """What an apply would do, for the adapter to show the owner."""
        found = self._journal().get_preview(_preview_id(preview_id))
        if found is None or found.caller != caller:
            raise bounds.refused("no such preview")
        return {
            "preview_id": found.id,
            "operation": found.operation,
            "summary": found.payload["summary"],
            "status": found.status,
            "expires_at": found.expires,
        }

    def store_preview(self, operation: str, caller: str, payload: dict[str, Any], summary: str) -> dict[str, Any]:
        payload = {**payload, "summary": summary}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        preview = Preview(
            id="p" + secrets.token_hex(12),
            operation=operation,
            caller=caller,
            expires=self._wall() + PREVIEW_TTL_SECONDS,
            hash=digest,
            payload=payload,
            status="open",
        )
        self._journal().put_preview(preview)
        return {"preview_id": preview.id, "summary": summary, "expires_at": preview.expires, **_visible(payload)}

    def take_preview(self, operation: str, preview_id: Any, approval: Any, caller: str) -> Preview:
        if approval not in APPROVALS:
            raise bounds.refused("an apply needs the owner's approval")
        found = self._journal().get_preview(_preview_id(preview_id))
        if found is None or found.caller != caller or found.operation != operation:
            raise bounds.refused("no such preview for this operation")
        if found.expires <= self._wall():
            self._journal().move_preview(found.id, from_status="open", to_status="expired")
            raise bounds.refused("the preview expired; prepare again")
        if not self._journal().move_preview(found.id, from_status="open", to_status="applying"):
            raise bounds.refused("the preview was already used")
        return found

    def _states(self, ids: list[str], deadline: float, *, tolerate: bool = False) -> list[frozenset[str] | None]:
        def read(message_id: str) -> frozenset[str] | None:
            try:
                reply = self._transport.read(
                    f"messages/{message_id}", {"format": "minimal", "fields": "id,labelIds"}, deadline=deadline
                )
            except GateError as error:
                if tolerate or error.category is Category.GMAIL_NOT_FOUND:
                    return None
                raise
            labels = reply.get("labelIds", [])
            if reply.get("id") != message_id or not isinstance(labels, list):
                return None
            return frozenset(label for label in labels if isinstance(label, str))

        with ThreadPoolExecutor(max_workers=self._parallel) as pool:
            return list(pool.map(read, ids))

    def _labels(self) -> dict[str, dict[str, str]]:
        return {label["name"]: label for label in self._reads.labels()["labels"]}

    def _label_by_name(self, name: str) -> dict[str, str] | None:
        return self._labels().get(name)

    def _resolve(self, names: list[str]) -> list[str]:
        labels = self._labels()
        ids = []
        for name in names:
            if name in MODIFIABLE_SYSTEM_LABELS:
                ids.append(name)
                continue
            label = labels.get(name)
            if label is None or label["type"] != "user":
                raise bounds.refused(
                    "labels must be existing user labels or one of " + ", ".join(sorted(MODIFIABLE_SYSTEM_LABELS))
                )
            ids.append(label["id"])
        return ids

    def _filter(self, filter_id: str) -> dict[str, Any] | None:
        for entry in self._reads.filters()["filters"]:
            if entry["id"] == filter_id:
                return entry
        return None


def _preview_id(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("p") or len(value) != 25:
        raise bounds.refused("preview_id is malformed")
    return bounds.gmail_id(value, "preview_id")


def _label_name(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > _LABEL_NAME_MAX
        or "\n" in value
        or "\r" in value
        or value.upper() in MODIFIABLE_SYSTEM_LABELS | UNTOUCHABLE | {"SENT", "DRAFT"}
    ):
        raise bounds.refused("label name is empty, too long, a system name or has line breaks")
    return value


def _names(value: Any, what: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and 0 < len(v) <= _LABEL_NAME_MAX for v in value):
        raise bounds.refused(f"{what} must be a list of label names")
    if len(set(value)) != len(value):
        raise bounds.refused(f"{what} names a label twice")
    return value


def _criteria(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value or set(value) - FILTER_CRITERIA:
        raise bounds.refused(f"criteria must use only {', '.join(sorted(FILTER_CRITERIA))}")
    for key, item in value.items():
        if key in {"hasAttachment", "excludeChats"}:
            if not isinstance(item, bool):
                raise bounds.refused(f"criteria {key} must be true or false")
        elif key == "size":
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise bounds.refused("criteria size must be a byte count")
        elif key == "sizeComparison":
            bounds.choice(item, "criteria sizeComparison", ("larger", "smaller"))
        elif not isinstance(item, str) or not item or len(item.encode()) > bounds.QUERY_MAX_BYTES:
            raise bounds.refused(f"criteria {key} must be a non-empty string")
    return dict(value)


def _same_filter(entry: dict[str, Any], criteria: dict[str, Any], action: dict[str, Any]) -> bool:
    """Compare label arrays as sets: Gmail may return them in another order."""
    stored_action = entry.get("action") or {}
    return (
        {k: v for k, v in (entry.get("criteria") or {}).items() if v not in (None, "", False)}
        == {k: v for k, v in criteria.items() if v not in (None, "", False)}
        and set(stored_action.get("addLabelIds") or []) == set(action.get("addLabelIds") or [])
        and set(stored_action.get("removeLabelIds") or []) == set(action.get("removeLabelIds") or [])
        and not stored_action.get("forward")
    )


def _describe(criteria: dict[str, Any]) -> str:
    return ", ".join(f"{key} {value}" for key, value in sorted(criteria.items()))


def _visible(payload: dict[str, Any]) -> dict[str, Any]:
    """What prepare shows: targets and changes, not the stored before-state."""
    return {key: value for key, value in payload.items() if key not in {"before", "summary"}}
