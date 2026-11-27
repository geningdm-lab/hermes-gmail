"""The write journal: what was changed, by whom and under which authority.

It records operations, callers, rule versions, preview hashes, approvals, statuses, Gmail
IDs and counts. It never records bodies, subjects, addresses, snippets, codes or tokens;
previews, which hold some of those, stay in the service's memory.
"""

import json
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS operations (
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL,
    caller TEXT NOT NULL,
    operation TEXT NOT NULL,
    status TEXT NOT NULL,
    rule_version TEXT,
    preview_hash TEXT,
    approval TEXT,
    gmail_ids TEXT NOT NULL DEFAULT '[]',
    counts TEXT NOT NULL DEFAULT '{}'
);
"""


class Journal:
    def __init__(self, path: Path, *, retention_days: float, wall_clock: Callable[[], float] = time.time) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self._wall = wall_clock
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.executescript(_SCHEMA)
        self._db.execute("PRAGMA synchronous = FULL")
        horizon = self._wall() - retention_days * 86400
        self._db.execute("DELETE FROM operations WHERE at < ?", (horizon,))

    def record(
        self,
        *,
        caller: str,
        operation: str,
        status: str,
        rule_version: str | None = None,
        preview_hash: str | None = None,
        approval: str | None = None,
        gmail_ids: Iterable[str] = (),
        counts: dict[str, int] | None = None,
    ) -> int:
        with self._lock:
            cursor = self._db.execute(
                "INSERT INTO operations"
                " (at, caller, operation, status, rule_version, preview_hash, approval, gmail_ids, counts)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self._wall(),
                    caller,
                    operation,
                    status,
                    rule_version,
                    preview_hash,
                    approval,
                    json.dumps(list(gmail_ids)),
                    json.dumps(counts or {}),
                ),
            )
            return int(cursor.lastrowid or 0)

    def update(self, entry_id: int, *, status: str, gmail_ids: Iterable[str] | None = None) -> None:
        with self._lock:
            if gmail_ids is None:
                self._db.execute("UPDATE operations SET status = ? WHERE id = ?", (status, entry_id))
            else:
                self._db.execute(
                    "UPDATE operations SET status = ?, gmail_ids = ? WHERE id = ?",
                    (status, json.dumps(list(gmail_ids)), entry_id),
                )

    def entries(self, operation: str | None = None) -> list[dict[str, Any]]:
        query = (
            "SELECT id, at, caller, operation, status, rule_version, preview_hash, approval, gmail_ids, counts"
            " FROM operations"
        )
        args: tuple[str, ...] = ()
        if operation is not None:
            query += " WHERE operation = ?"
            args = (operation,)
        with self._lock:
            rows = self._db.execute(query + " ORDER BY id", args).fetchall()
        keys = ("id", "at", "caller", "operation", "status", "rule_version", "preview_hash", "approval")
        return [
            {**dict(zip(keys, row[:8], strict=True)), "gmail_ids": json.loads(row[8]), "counts": json.loads(row[9])}
            for row in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._db.close()
