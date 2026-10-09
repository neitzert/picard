"""Write-ahead journal for crash recovery during put operations.

The journal table lives in the same SQLite database as the Index,
sharing its connection for transactional atomicity.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


class JournalState(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"


@dataclass(slots=True)
class JournalEntry:
    journal_id: str
    operation: str
    path: str
    manifest_id: str
    state: JournalState
    created_at: str
    metadata: dict


_JOURNAL_SCHEMA = """\
CREATE TABLE IF NOT EXISTS journal_entries (
    journal_id  TEXT PRIMARY KEY,
    operation   TEXT NOT NULL,
    path        TEXT NOT NULL,
    manifest_id TEXT,
    state       TEXT NOT NULL DEFAULT 'pending',
    created_at  TEXT NOT NULL,
    metadata    TEXT NOT NULL DEFAULT '{}'
);
"""


class Journal:
    """WAL backed by a SQLite table."""

    def __init__(
        self, conn: sqlite3.Connection, *, write_lock: threading.Lock | None = None,
    ) -> None:
        self._conn = conn
        self._conn.row_factory = sqlite3.Row
        self._lock = write_lock or threading.Lock()
        self._conn.executescript(_JOURNAL_SCHEMA)

    def begin(self, operation: str, path: str, manifest_id: str) -> str:
        jid = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO journal_entries VALUES (?,?,?,?,?,?,?)",
                (jid, operation, path, manifest_id, JournalState.PENDING.value, now, "{}"),
            )
        return jid

    def mark_in_progress(self, journal_id: str, metadata: dict | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE journal_entries SET state=?, metadata=? WHERE journal_id=?",
                (JournalState.IN_PROGRESS.value, json.dumps(metadata or {}), journal_id),
            )

    def update_metadata(self, journal_id: str, metadata: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE journal_entries SET metadata=? WHERE journal_id=?",
                (json.dumps(metadata), journal_id),
            )

    def commit(self, journal_id: str) -> None:
        with self._lock, self._conn:
            self._commit_raw(journal_id)

    def _commit_raw(self, journal_id: str) -> None:
        """Execute commit SQL without transaction wrapper."""
        self._conn.execute(
            "UPDATE journal_entries SET state=? WHERE journal_id=?",
            (JournalState.COMMITTED.value, journal_id),
        )

    def rollback(self, journal_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE journal_entries SET state=? WHERE journal_id=?",
                (JournalState.ROLLED_BACK.value, journal_id),
            )

    def get_incomplete(self) -> list[JournalEntry]:
        rows = self._conn.execute(
            "SELECT * FROM journal_entries WHERE state IN (?, ?) ORDER BY created_at",
            (JournalState.PENDING.value, JournalState.IN_PROGRESS.value),
        ).fetchall()
        return [
            JournalEntry(
                journal_id=r["journal_id"],
                operation=r["operation"],
                path=r["path"],
                manifest_id=r["manifest_id"],
                state=JournalState(r["state"]),
                created_at=r["created_at"],
                metadata=json.loads(r["metadata"]),
            )
            for r in rows
        ]

    def cleanup_committed(self, max_age_hours: int = 0) -> int:
        """Remove completed journal entries older than *max_age_hours*.

        Default of 0 cleans up all committed/rolled-back entries immediately.
        Pass a positive value to retain recent entries for auditing.
        """
        if max_age_hours <= 0:
            with self._lock, self._conn:
                cur = self._conn.execute(
                    "DELETE FROM journal_entries WHERE state IN (?, ?)",
                    (JournalState.COMMITTED.value, JournalState.ROLLED_BACK.value),
                )
                return cur.rowcount
        from datetime import timedelta
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        ).isoformat()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM journal_entries WHERE state IN (?, ?) "
                "AND created_at < ?",
                (
                    JournalState.COMMITTED.value,
                    JournalState.ROLLED_BACK.value,
                    cutoff,
                ),
            )
            return cur.rowcount
