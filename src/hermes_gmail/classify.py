"""`classify_apply`: the classifier's unattended label changes (docs/design.md, Classify).

The plan is checked whole before any write: only existing user labels may be added and
only INBOX removed; a protected message or a result without exactly one exclusive label
refuses the plan. Writes go out per label delta in batches, and every written message is
read back. Each message then lands in exactly one outcome list, so the consumer can
advance its checkpoint for the verified ones and set the rest aside.
"""

import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from hermes_gmail import bounds
from hermes_gmail.errors import Category, GateError
from hermes_gmail.health import Health
from hermes_gmail.journal import Journal
from hermes_gmail.reads import Reads
from hermes_gmail.transport import Transport

PLAN_MAX = 2000
BATCH_MAX = 1000
PROTECTED = frozenset({"SPAM", "TRASH", "SENT", "DRAFT"})
_RULE_VERSION = re.compile(r"[A-Za-z0-9._:-]{1,100}")
_LABEL_NAME_MAX = 225
OUTCOMES = ("missing", "unchanged", "verified", "mismatched", "unverified", "not_attempted")


class Classifier:
    def __init__(
        self,
        *,
        transport: Transport,
        reads: Reads,
        journal: Callable[[], Journal],
        health: Health,
        exclusive_prefix: str | None,
        parallel: int,
        deadline: Callable[[float], float],
    ) -> None:
        self._transport = transport
        self._reads = reads
        self._journal = journal
        self._health = health
        self._prefix = exclusive_prefix
        self._parallel = parallel
        self._deadline = deadline

    def apply(self, plan: Any, rule_version: Any, caller: str = "local") -> dict[str, Any]:
        entries = _parse_plan(plan)
        if not isinstance(rule_version, str) or not _RULE_VERSION.fullmatch(rule_version):
            raise bounds.refused("rule_version is missing or malformed")
        if not self._prefix:
            raise bounds.refused("classification has no exclusive label prefix configured")
        outcome: dict[str, list[str]] = {name: [] for name in OUTCOMES}
        if not entries:
            return self._finish(outcome, rule_version, caller, 0, None)
        deadline = self._deadline(120 + 0.5 * len(entries))

        labels = self._reads.labels()["labels"]
        by_name = {label["name"]: label for label in labels}
        exclusive = {label["id"] for label in labels if label["name"].startswith(self._prefix)}

        before = dict(zip(entries, self._read_states(list(entries), deadline), strict=True))
        desired: dict[str, frozenset[str]] = {}
        groups: dict[tuple[tuple[str, ...], tuple[str, ...]], list[str]] = {}
        for message_id, (add_names, remove) in entries.items():
            old = before[message_id]
            if old is None:
                outcome["missing"].append(message_id)
                continue
            if old & PROTECTED:
                raise bounds.refused("the plan includes a spam, trash, sent or draft message")
            add = set()
            for name in add_names:
                label = by_name.get(name)
                if label is None or label["type"] != "user":
                    raise bounds.refused("the plan adds a label that is not an existing user label")
                add.add(label["id"])
            new = frozenset((old | add) - remove)
            if len(new & exclusive) != 1:
                raise bounds.refused(f"a message would not carry exactly one {self._prefix} label")
            desired[message_id] = new
            if new == old:
                outcome["unchanged"].append(message_id)
            else:
                groups.setdefault((tuple(sorted(new - old)), tuple(sorted(old - new))), []).append(message_id)

        stopped: Category | None = None
        batches = [
            (add, remove, ids[offset : offset + BATCH_MAX])
            for (add, remove), ids in groups.items()
            for offset in range(0, len(ids), BATCH_MAX)
        ]
        for index, (add, remove, batch) in enumerate(batches):
            try:
                self._transport.write(
                    "POST",
                    "messages/batchModify",
                    {"ids": batch, "addLabelIds": list(add), "removeLabelIds": list(remove)},
                    deadline=deadline,
                )
            except GateError as error:
                # Whatever Gmail did with this batch, the read-back below says what is true.
                stopped = error.category
            for message_id, state in zip(batch, self._read_states(batch, deadline, tolerate=True), strict=True):
                if state is None:
                    outcome["unverified"].append(message_id)
                elif state == desired[message_id] and ("UNREAD" in state) == ("UNREAD" in before[message_id]):
                    outcome["verified"].append(message_id)
                else:
                    outcome["mismatched"].append(message_id)  # a race; never rolled back
            if stopped is not None:
                for _, _, rest in batches[index + 1 :]:
                    outcome["not_attempted"].extend(rest)
                break
        return self._finish(outcome, rule_version, caller, len(entries), stopped)

    def _read_states(self, ids: list[str], deadline: float, *, tolerate: bool = False) -> list[frozenset[str] | None]:
        """Label sets by fresh minimal reads. None: gone (404), or unreadable when tolerated."""

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
                if tolerate:
                    return None
                raise GateError(Category.GMAIL_FAILED, "Gmail's message state reply has an unexpected shape")
            return frozenset(label for label in labels if isinstance(label, str))

        with ThreadPoolExecutor(max_workers=self._parallel) as pool:
            return list(pool.map(read, ids))

    def _finish(
        self,
        outcome: dict[str, list[str]],
        rule_version: str,
        caller: str,
        planned: int,
        stopped: Category | None,
    ) -> dict[str, Any]:
        clean = stopped is None and not (outcome["mismatched"] or outcome["unverified"] or outcome["not_attempted"])
        self._journal().record(
            caller=caller,
            operation="classify_apply",
            status="complete" if clean else "partial",
            rule_version=rule_version,
            gmail_ids=outcome["verified"] + outcome["mismatched"] + outcome["unverified"],
            counts={name: len(ids) for name, ids in outcome.items()} | {"planned": planned},
        )
        if clean:
            self._health.success("classify")
        return {"rule_version": rule_version, "planned": planned, "stopped": stopped and str(stopped), **outcome}


def _parse_plan(plan: Any) -> dict[str, tuple[list[str], frozenset[str]]]:
    if not isinstance(plan, list):
        raise bounds.refused("plan must be a list")
    if len(plan) > PLAN_MAX:
        raise bounds.refused(f"plan has more than {PLAN_MAX} entries")
    entries: dict[str, tuple[list[str], frozenset[str]]] = {}
    for item in plan:
        if not isinstance(item, dict) or set(item) != {"id", "add", "remove"}:
            raise bounds.refused("a plan entry must have exactly id, add and remove")
        message_id = bounds.gmail_id(item["id"], "plan entry id")
        if message_id in entries:
            raise bounds.refused("the plan names a message twice")
        add, remove = item["add"], item["remove"]
        for names in (add, remove):
            if not isinstance(names, list) or not all(
                isinstance(n, str) and 0 < len(n) <= _LABEL_NAME_MAX for n in names
            ):
                raise bounds.refused("add and remove must be lists of label names")
        if set(remove) - {"INBOX"}:
            raise bounds.refused("a plan may only remove INBOX")
        entries[message_id] = (add, frozenset(remove))
    return entries
