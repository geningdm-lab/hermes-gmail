"""Read operations (docs/design.md, Operations: Read).

Every count in a result is what was fetched, never Gmail's `resultSizeEstimate`. A result
that stops at a bound says so with `complete: false` and the token to continue.
"""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from hermes_gmail import bounds
from hermes_gmail.errors import Category, GateError
from hermes_gmail.mime import TEXT_CAP_DEFAULT, TEXT_CAP_MAX, extract_text
from hermes_gmail.transport import Transport

READ_DEADLINE_SECONDS = 120.0
_LIST_PAGE_SIZE = 500
_FORMATS = ("minimal", "metadata", "full")
REPLY_HEADERS = ("From", "To", "Cc", "Date", "Subject", "Message-ID", "References", "In-Reply-To")


class Reads:
    def __init__(self, transport: Transport, *, parallel: int, deadline: Callable[[float], float]) -> None:
        self._transport = transport
        self._parallel = parallel
        self._deadline = deadline

    def profile(self) -> dict[str, Any]:
        reply = self._get("profile")
        account, history = reply.get("emailAddress"), reply.get("historyId")
        if not isinstance(account, str) or not isinstance(history, str):
            raise _malformed("profile")
        return {"account": account, "history_id": history}

    def search(self, query: Any, page_bound: Any = 1, page_token: Any = None) -> dict[str, Any]:
        q = bounds.query(query)
        pages = bounds.count(page_bound, "page_bound", bounds.PAGE_BOUND_MAX)
        token = bounds.page_token(page_token)
        deadline = self._deadline(READ_DEADLINE_SECONDS)
        ids: list[str] = []
        seen_ids: set[str] = set()
        seen_tokens: set[str] = {token} if token else set()
        for _ in range(pages):
            params: dict[str, Any] = {"q": q, "maxResults": _LIST_PAGE_SIZE}
            if token:
                params["pageToken"] = token
            page = self._transport.read("messages", params, deadline=deadline)
            for item in _list(page, "messages"):
                message_id = item.get("id") if isinstance(item, dict) else None
                if not isinstance(message_id, str):
                    raise _malformed("messages.list")
                if message_id not in seen_ids:
                    seen_ids.add(message_id)
                    ids.append(message_id)
            token = page.get("nextPageToken")
            if not token:
                return {"ids": ids, "fetched": len(ids), "complete": True}
            if not isinstance(token, str) or token in seen_tokens:
                raise GateError(Category.GMAIL_FAILED, "Gmail repeated a page token")
            seen_tokens.add(token)
        return {"ids": ids, "fetched": len(ids), "complete": False, "next_page_token": token}

    def read_messages(
        self,
        ids: Any,
        format: Any = "metadata",
        headers: Any = None,
        cap: Any = TEXT_CAP_DEFAULT,
    ) -> dict[str, Any]:
        """Each requested ID ends in `messages` or in `failed` with its category."""
        message_ids = bounds.gmail_ids(ids)
        fmt = bounds.choice(format, "format", _FORMATS)
        names = _header_names(headers)
        text_cap = bounds.count(cap, "cap", TEXT_CAP_MAX)
        deadline = self._deadline(READ_DEADLINE_SECONDS)

        def read(message_id: str) -> dict[str, Any]:
            params: dict[str, Any] = {"format": fmt}
            if fmt == "metadata" and names:
                params["metadataHeaders"] = names
            try:
                reply = self._transport.read(f"messages/{message_id}", params, deadline=deadline)
                return {"ok": message_view(reply, message_id, fmt, text_cap)}
            except GateError as error:
                return {"failed": {"id": message_id, "category": str(error.category)}}

        with ThreadPoolExecutor(max_workers=self._parallel) as pool:
            outcomes = list(pool.map(read, message_ids))
        messages = [o["ok"] for o in outcomes if "ok" in o]
        failed = [o["failed"] for o in outcomes if "failed" in o]
        return {"messages": messages, "failed": failed, "fetched": len(messages)}

    def read_thread(self, thread_id: Any, bound: Any = 20, cap: Any = TEXT_CAP_DEFAULT) -> dict[str, Any]:
        """The thread's latest `bound` messages, oldest first, with direction and reply headers.

        Gmail lists a thread's drafts among its messages; their direction is `draft`, so a
        reply is never addressed to an unsent draft."""
        tid = bounds.gmail_id(thread_id, "thread_id")
        limit = bounds.count(bound, "bound", bounds.THREAD_BOUND_MAX)
        text_cap = bounds.count(cap, "cap", TEXT_CAP_MAX)
        reply = self._get(f"threads/{tid}", {"format": "full"})
        if reply.get("id") != tid:
            raise _malformed("threads.get")
        raw = _list(reply, "messages")
        messages = []
        for item in raw[-limit:]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise _malformed("threads.get")
            view = message_view(item, item["id"], "full", text_cap)
            view["direction"] = _direction(view["label_ids"])
            view["headers"] = [h for h in view["headers"] if h["name"].lower() in _REPLY_HEADER_KEYS]
            messages.append(view)
        return {"thread_id": tid, "messages": messages, "complete": len(raw) <= limit, "total": len(raw)}

    def history(self, start_history_id: Any, page_bound: Any = 10, page_token: Any = None) -> dict[str, Any]:
        """Changes since `start_history_id`. Advance a cursor to `history_id` only when
        `complete` is true. `gmail_not_found` means the start is too old: resync."""
        start = bounds.history_id(start_history_id)
        pages = bounds.count(page_bound, "page_bound", bounds.PAGE_BOUND_MAX)
        token = bounds.page_token(page_token)
        deadline = self._deadline(READ_DEADLINE_SECONDS)
        added: list[str] = []
        deleted: list[str] = []
        labels_added: list[dict[str, Any]] = []
        labels_removed: list[dict[str, Any]] = []
        seen_tokens: set[str] = {token} if token else set()
        current = start
        for _ in range(pages):
            params: dict[str, Any] = {"startHistoryId": start, "maxResults": _LIST_PAGE_SIZE}
            if token:
                params["pageToken"] = token
            page = self._transport.read("history", params, deadline=deadline)
            current = page.get("historyId", current)
            if not isinstance(current, str):
                raise _malformed("history.list")
            for record in _list(page, "history"):
                if not isinstance(record, dict):
                    raise _malformed("history.list")
                added += [_message_id(entry) for entry in _list(record, "messagesAdded")]
                deleted += [_message_id(entry) for entry in _list(record, "messagesDeleted")]
                labels_added += [_label_change(entry) for entry in _list(record, "labelsAdded")]
                labels_removed += [_label_change(entry) for entry in _list(record, "labelsRemoved")]
            token = page.get("nextPageToken")
            if not token:
                break
            if not isinstance(token, str) or token in seen_tokens:
                raise GateError(Category.GMAIL_FAILED, "Gmail repeated a page token")
            seen_tokens.add(token)
        result: dict[str, Any] = {
            "history_id": current,
            "messages_added": _unique(added),
            "messages_deleted": _unique(deleted),
            "labels_added": labels_added,
            "labels_removed": labels_removed,
            "complete": not token,
        }
        if token:
            result["next_page_token"] = token
        return result

    def labels(self) -> dict[str, Any]:
        items = []
        for label in _list(self._get("labels"), "labels"):
            if not isinstance(label, dict) or not all(isinstance(label.get(k), str) for k in ("id", "name", "type")):
                raise _malformed("labels.list")
            items.append({"id": label["id"], "name": label["name"], "type": label["type"]})
        return {"labels": items}

    def filters(self) -> dict[str, Any]:
        items = []
        for entry in _list(self._get("settings/filters"), "filter"):
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                raise _malformed("settings.filters.list")
            items.append(
                {"id": entry["id"], "criteria": _dict(entry.get("criteria")), "action": _dict(entry.get("action"))}
            )
        return {"filters": items}

    def drafts(self, thread_id: Any = None, page_bound: Any = 10) -> dict[str, Any]:
        tid = None if thread_id is None else bounds.gmail_id(thread_id, "thread_id")
        pages = bounds.count(page_bound, "page_bound", bounds.PAGE_BOUND_MAX)
        deadline = self._deadline(READ_DEADLINE_SECONDS)
        items: list[dict[str, str]] = []
        token: str | None = None
        seen_tokens: set[str] = set()
        for _ in range(pages):
            params: dict[str, Any] = {"maxResults": _LIST_PAGE_SIZE}
            if token:
                params["pageToken"] = token
            page = self._transport.read("drafts", params, deadline=deadline)
            for draft in _list(page, "drafts"):
                message = draft.get("message") if isinstance(draft, dict) else None
                if not isinstance(message, dict) or not all(
                    isinstance(v, str) for v in (draft.get("id"), message.get("id"), message.get("threadId"))
                ):
                    raise _malformed("drafts.list")
                if tid is None or message["threadId"] == tid:
                    items.append({"id": draft["id"], "message_id": message["id"], "thread_id": message["threadId"]})
            token = page.get("nextPageToken")
            if not token:
                return {"drafts": items, "complete": True}
            if not isinstance(token, str) or token in seen_tokens:
                raise GateError(Category.GMAIL_FAILED, "Gmail repeated a page token")
            seen_tokens.add(token)
        return {"drafts": items, "complete": False}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._transport.read(path, params, deadline=self._deadline(READ_DEADLINE_SECONDS))


_REPLY_HEADER_KEYS = frozenset(name.lower() for name in REPLY_HEADERS)


def message_view(reply: dict[str, Any], expected_id: str, fmt: str, cap: int) -> dict[str, Any]:
    """Parse one messages.get reply; a reply for another ID or of the wrong shape fails."""
    if reply.get("id") != expected_id:
        raise _malformed("messages.get")
    label_ids = reply.get("labelIds", [])
    if not isinstance(label_ids, list) or not all(isinstance(label, str) for label in label_ids):
        raise _malformed("messages.get")
    view: dict[str, Any] = {"id": expected_id, "thread_id": reply.get("threadId"), "label_ids": label_ids}
    if fmt == "minimal":
        return view
    payload = reply.get("payload") if isinstance(reply.get("payload"), dict) else {}
    raw_headers = payload.get("headers", [])
    if not isinstance(raw_headers, list):
        raise _malformed("messages.get")
    view["headers"] = [
        {"name": h["name"], "value": h["value"]}
        for h in raw_headers
        if isinstance(h, dict) and isinstance(h.get("name"), str) and isinstance(h.get("value"), str)
    ]
    view["internal_date"] = reply.get("internalDate")
    view["snippet"] = reply.get("snippet", "")
    if fmt == "full":
        view.update(extract_text(payload, cap).to_dict())
    return view


def _direction(label_ids: list[str]) -> str:
    if "DRAFT" in label_ids:
        return "draft"
    return "sent" if "SENT" in label_ids else "received"


def _header_names(value: Any) -> list[str]:
    if value is None:
        return []
    if (
        not isinstance(value, list)
        or len(value) > 50
        or not all(isinstance(v, str) and 0 < len(v) <= 100 for v in value)
    ):
        raise bounds.refused("headers must be a list of at most 50 header names")
    return value


def _list(reply: dict[str, Any], key: str) -> list[Any]:
    value = reply.get(key, [])
    if not isinstance(value, list):
        raise _malformed(key)
    return value


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _message_id(entry: Any) -> str:
    message = entry.get("message") if isinstance(entry, dict) else None
    if not isinstance(message, dict) or not isinstance(message.get("id"), str):
        raise _malformed("history.list")
    return message["id"]


def _label_change(entry: Any) -> dict[str, Any]:
    label_ids = entry.get("labelIds") if isinstance(entry, dict) else None
    if not isinstance(label_ids, list) or not all(isinstance(label, str) for label in label_ids):
        raise _malformed("history.list")
    return {"id": _message_id(entry), "label_ids": label_ids}


def _unique(ids: list[str]) -> list[str]:
    return list(dict.fromkeys(ids))


def _malformed(what: str) -> GateError:
    return GateError(Category.GMAIL_FAILED, f"Gmail's {what} reply has an unexpected shape")
