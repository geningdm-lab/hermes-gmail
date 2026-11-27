"""The write journal: what was changed, by whom and under which authority.

It records operations, callers, rule versions, statuses, Gmail IDs and counts. It never
records bodies, subjects, snippets, codes or tokens.
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
        self._db.execute("DELETE FROM operations WHERE at < ?", (self._wall() - retention_days * 86400,))

    def record(
        self,
        *,
        caller: str,
        operation: str,
        status: str,
        rule_version: str | None = None,
        gmail_ids: Iterable[str] = (),
        counts: dict[str, int] | None = None,
    ) -> int:
        with self._lock:
            cursor = self._db.execute(
                "INSERT INTO operations (at, caller, operation, status, rule_version, gmail_ids, counts)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    self._wall(),
                    caller,
                    operation,
                    status,
                    rule_version,
                    json.dumps(list(gmail_ids)),
                    json.dumps(counts or {}),
                ),
            )
            return int(cursor.lastrowid or 0)

    def entries(self, operation: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT id, at, caller, operation, status, rule_version, gmail_ids, counts FROM operations"
        args: tuple[str, ...] = ()
        if operation is not None:
            query += " WHERE operation = ?"
            args = (operation,)
        with self._lock:
            rows = self._db.execute(query + " ORDER BY id", args).fetchall()
        return [
            {
                "id": row[0],
                "at": row[1],
                "caller": row[2],
                "operation": row[3],
                "status": row[4],
                "rule_version": row[5],
                "gmail_ids": json.loads(row[6]),
                "counts": json.loads(row[7]),
            }
            for row in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._db.close()
