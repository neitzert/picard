"""Local SQLite adapter — stores chunks in a local SQLite database.

Each chunk is a row in a 'chunks' table, keyed by a UUID.
Uses Python's built-in sqlite3 module — no external deps.

Different from the sqlite_remote adapter which targets a remote
SQLite-over-HTTP service.
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class SQLiteLocalAdapter(Adapter):
    """Scatter chunks into a local SQLite database."""

    def __init__(
        self,
        db_path: str = ":memory:",
        *,
        max_chunk_bytes: int = 65536,
    ) -> None:
        self._db_path = db_path
        self._max = max_chunk_bytes
        self._conn = sqlite3.connect(db_path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS chunks "
            "(id TEXT PRIMARY KEY, manifest_id TEXT, data TEXT)"
        )
        self._conn.commit()

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="sqlite_local",
            max_chunk_bytes=self._max,
            persistent=self._db_path != ":memory:",
            ordered=True,
            rate_limit_per_min=1000,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_id = uuid.uuid4().hex[:12]
        location = f"sqlite://{self._db_path}/{chunk_id}"
        try:
            from picard.chunker import Chunk
            manifest_id = Chunk.decode(encoded_chunk).manifest_id
        except Exception:
            manifest_id = ""
        self._conn.execute(
            "INSERT INTO chunks (id, manifest_id, data) VALUES (?, ?, ?)",
            (chunk_id, manifest_id, encoded_chunk),
        )
        self._conn.commit()
        return StoredChunkRef(adapter_name="sqlite_local", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        chunk_id = ref.location.rsplit("/", 1)[-1]
        row = self._conn.execute(
            "SELECT data FROM chunks WHERE id = ?", (chunk_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"chunk not found: {ref.location}")
        return row[0]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        chunk_id = ref.location.rsplit("/", 1)[-1]
        cursor = self._conn.execute(
            "DELETE FROM chunks WHERE id = ?", (chunk_id,)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        rows = self._conn.execute(
            "SELECT id FROM chunks WHERE manifest_id = ?", (manifest_id,)
        ).fetchall()
        return [
            StoredChunkRef(
                adapter_name="sqlite_local",
                location=f"sqlite://{self._db_path}/{row[0]}",
            )
            for row in rows
        ]

    async def health_check(self) -> bool:
        try:
            self._conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        row = self._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
        chunk_count = row[0] if row else 0
        size_row = self._conn.execute(
            "SELECT COALESCE(SUM(LENGTH(data)), 0) FROM chunks"
        ).fetchone()
        used = size_row[0] if size_row else 0
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": chunk_count,
        }
