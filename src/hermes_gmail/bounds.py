"""Checks every operation applies to its arguments before any vault or provider call.

A request outside these bounds is `refused`. The values are this gate's policy, not
documented Gmail limits.
"""

import re
from collections.abc import Sequence
from typing import Any

from hermes_gmail.errors import Category, GateError

QUERY_MAX_BYTES = 8 * 1024
ID_MAX_BYTES = 512
PAGE_BOUND_MAX = 100
IDS_MAX = 100
THREAD_BOUND_MAX = 100
_ID = re.compile(r"[A-Za-z0-9_-]+")
_HISTORY_ID = re.compile(r"[0-9]{1,30}")


def refused(detail: str) -> GateError:
    return GateError(Category.REFUSED, detail)


def gmail_id(value: Any, what: str = "id") -> str:
    """A message, thread, draft, label or filter ID: Gmail's alphabet, bounded length."""
    if not isinstance(value, str) or len(value.encode()) > ID_MAX_BYTES or not _ID.fullmatch(value):
        raise refused(f"{what} is not a Gmail ID")
    return value


def gmail_ids(values: Any, what: str = "ids", maximum: int = IDS_MAX) -> list[str]:
    if not isinstance(values, list) or not values:
        raise refused(f"{what} must be a non-empty list")
    if len(values) > maximum:
        raise refused(f"{what} has more than {maximum} entries")
    ids = [gmail_id(value, what) for value in values]
    if len(set(ids)) != len(ids):
        raise refused(f"{what} has duplicates")
    return ids


def query(value: Any) -> str:
    if not isinstance(value, str):
        raise refused("query must be a string")
    if len(value.encode()) > QUERY_MAX_BYTES:
        raise refused(f"query is longer than {QUERY_MAX_BYTES} bytes")
    return value


def count(value: Any, what: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise refused(f"{what} must be an integer from 1 to {maximum}")
    return value


def history_id(value: Any) -> str:
    text = str(value) if isinstance(value, int) and not isinstance(value, bool) else value
    if not isinstance(text, str) or not _HISTORY_ID.fullmatch(text):
        raise refused("start_history_id is not a Gmail history ID")
    return text


def page_token(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value.encode()) > ID_MAX_BYTES:
        raise refused("page_token is malformed")
    return value


def choice(value: Any, what: str, allowed: Sequence[str]) -> str:
    if value not in allowed:
        raise refused(f"{what} must be one of {', '.join(allowed)}")
    return value
