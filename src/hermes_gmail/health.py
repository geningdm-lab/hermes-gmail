"""Counters a caller or a watchdog can read without seeing mail or secrets."""

import threading
import time
from collections.abc import Callable
from typing import Any

from hermes_gmail.errors import Category, GateError


class Health:
    def __init__(self, wall_clock: Callable[[], float] = time.time) -> None:
        self._lock = threading.Lock()
        self._wall = wall_clock
        self._errors = dict.fromkeys(Category, 0)
        self._events = {"vault_reads": 0, "token_refreshes": 0, "requests": 0}
        self._last_success: dict[str, float] = {}

    def fail(self, category: Category, detail: str) -> GateError:
        """Count the category and return the error for the caller to raise."""
        with self._lock:
            self._errors[category] += 1
        return GateError(category, detail)

    def count(self, event: str) -> None:
        with self._lock:
            self._events[event] += 1

    def success(self, operation_class: str) -> None:
        with self._lock:
            self._last_success[operation_class] = self._wall()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "errors": {str(category): count for category, count in self._errors.items()},
                "events": dict(self._events),
                "last_success": dict(self._last_success),
            }
