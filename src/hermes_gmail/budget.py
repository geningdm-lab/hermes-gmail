"""One request budget per account, shared by every caller (D5)."""

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from hermes_gmail.errors import Category
from hermes_gmail.health import Health


class Budget:
    """Request starts at least `interval` seconds apart, at most `in_flight` at once."""

    def __init__(
        self,
        *,
        interval: float,
        in_flight: int,
        health: Health,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._interval = interval
        self._slots = threading.BoundedSemaphore(in_flight)
        self._in_flight_max = in_flight
        self._health = health
        self._monotonic = monotonic
        self._sleep = sleep
        self._pace_lock = threading.Lock()
        self._next_start = 0.0
        self._in_flight = 0
        self._count_lock = threading.Lock()

    @contextmanager
    def slot(self, deadline: float) -> Iterator[None]:
        remaining = deadline - self._monotonic()
        if remaining <= 0 or not self._slots.acquire(timeout=remaining):
            raise self._health.fail(Category.GMAIL_QUOTA, "the shared request budget had no slot in time")
        try:
            with self._pace_lock:
                delay = self._next_start - self._monotonic()
                if delay > 0:
                    self._sleep(delay)
                self._next_start = self._monotonic() + self._interval
            with self._count_lock:
                self._in_flight += 1
            try:
                yield
            finally:
                with self._count_lock:
                    self._in_flight -= 1
        finally:
            self._slots.release()

    def usage(self) -> dict[str, float | int]:
        with self._count_lock:
            return {"in_flight": self._in_flight, "in_flight_max": self._in_flight_max, "interval": self._interval}
