"""The composition root: one broker, one transport, one budget per process."""

import threading
import time
from collections.abc import Callable
from typing import Any

from hermes_gmail.broker import CooldownStore, CredentialBroker, TokenClient
from hermes_gmail.budget import Budget
from hermes_gmail.classify import Classifier
from hermes_gmail.config import Config
from hermes_gmail.errors import GateError
from hermes_gmail.exchange import Opener, default_opener
from hermes_gmail.health import Health
from hermes_gmail.journal import Journal
from hermes_gmail.reads import Reads
from hermes_gmail.transport import Transport
from hermes_gmail.vault import OpVault


class Gate:
    def __init__(
        self,
        config: Config,
        *,
        opener: Opener | None = None,
        wall_clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self._monotonic = monotonic
        self._wall = wall_clock
        self._journal: Journal | None = None
        self._journal_lock = threading.Lock()
        opener = opener or default_opener()
        self.health_counters = Health(wall_clock)
        self.broker = CredentialBroker(
            vault=OpVault(config.vault),
            tokens=TokenClient(opener, self.health_counters, config.google.token_url),
            cooldown=CooldownStore(config.state_dir),
            config=config.vault,
            health=self.health_counters,
            wall_clock=wall_clock,
            monotonic=monotonic,
            sleep=sleep,
        )
        self.budget = Budget(
            interval=config.budget.interval_seconds,
            in_flight=config.budget.in_flight,
            health=self.health_counters,
            monotonic=monotonic,
            sleep=sleep,
        )
        self.transport = Transport(
            base_url=config.google.gmail_base,
            broker=self.broker,
            budget=self.budget,
            health=self.health_counters,
            opener=opener,
            monotonic=monotonic,
            sleep=sleep,
        )
        self.reads = Reads(self.transport, parallel=config.budget.in_flight, deadline=self.deadline)
        self.classifier = Classifier(
            transport=self.transport,
            reads=self.reads,
            journal=self.journal,
            health=self.health_counters,
            exclusive_prefix=config.classify_exclusive_prefix,
            parallel=config.budget.in_flight,
            deadline=self.deadline,
        )

    def journal(self) -> Journal:
        """Opened on first write, so read-only use never touches the state directory."""
        with self._journal_lock:
            if self._journal is None:
                self._journal = Journal(
                    self.config.state_dir / "journal.sqlite",
                    retention_days=self.config.journal_retention_days,
                    wall_clock=self._wall,
                )
            return self._journal

    def close(self) -> None:
        with self._journal_lock:
            if self._journal is not None:
                self._journal.close()
                self._journal = None

    def deadline(self, seconds: float) -> float:
        return self._monotonic() + seconds

    def health(self, *, force_refresh: bool = False) -> dict[str, Any]:
        """Counters, last successes, cooldown and budget use; no mail content.

        With `force_refresh` the broker refreshes first, so a revoked refresh token
        shows up here instead of in the next mailbox call (the watchdog's check).
        """
        refresh: str | None = None
        if force_refresh:
            try:
                self.broker.force_refresh()
                refresh = "ok"
            except GateError as error:
                refresh = str(error.category)
        snapshot = self.health_counters.snapshot()
        snapshot["vault_cooldown_until"] = self.broker.cooldown_until()
        snapshot["budget"] = self.budget.usage()
        if refresh is not None:
            snapshot["forced_refresh"] = refresh
        return snapshot
