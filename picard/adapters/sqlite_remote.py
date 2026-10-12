"""SQLite adapter — stores chunks in a SQLite database.

Works with local SQLite files, in-memory databases, or remote
libSQL/Turso instances via compatible drivers.  Each chunk is a row
in a dedicated table.

Requires: pip install picard-store[sqlite]
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class SqliteRemoteAdapter(Adapter):
    """Scatter chunks into a SQLite database."""

    def __init__(
        self,
        db_path: str = ":memory:",
        *,
        table: str = "picard_chunks",
        max_chunk_bytes: int = 100_000_000,
    ) -> None:
        self._db_path = db_path
        self._table = table
        self._max = max_chunk_bytes
        self._db: object | None = None

    async def _ensure_db(self):
        if self._db is not None:
            return self._db
        import aiosqlite

        db = await aiosqlite.connect(self._db_path)
        await db.execute(
            f"CREATE TABLE IF NOT EXISTS {self._table} ("
            "  id TEXT PRIMARY KEY,"
            "  manifest_id TEXT NOT NULL,"
            "  block_index INTEGER NOT NULL,"
            "  chunk_index INTEGER NOT NULL,"
            "  data TEXT NOT NULL,"
            "  created_at TEXT NOT NULL DEFAULT (datetime('now'))"
            ")"
        )
        await db.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{self._table}_manifest "
            f"ON {self._table}(manifest_id)"
        )
        await db.commit()
        self._db = db
        return db

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="sqlite",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=10000,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        db = await self._ensure_db()
        chunk = Chunk.decode(encoded_chunk)
        chunk_id = uuid.uuid4().hex
        await db.execute(
            f"INSERT INTO {self._table} (id, manifest_id, block_index, chunk_index, data) "
            "VALUES (?, ?, ?, ?, ?)",
            (chunk_id, chunk.manifest_id, chunk.block_index, chunk.chunk_index, encoded_chunk),
        )
        await db.commit()
        return StoredChunkRef(adapter_name="sqlite", location=chunk_id)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        db = await self._ensure_db()
        cursor = await db.execute(
            f"SELECT data FROM {self._table} WHERE id = ?", (ref.location,)
        )
        row = await cursor.fetchone()
        if row is None:
            raise ValueError(f"chunk not found: {ref.location}")
        return row[0]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        db = await self._ensure_db()
        cursor = await db.execute(
            f"DELETE FROM {self._table} WHERE id = ?", (ref.location,)
        )
        await db.commit()
        return cursor.rowcount > 0

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        db = await self._ensure_db()
        cursor = await db.execute(
            f"SELECT id FROM {self._table} WHERE manifest_id = ? ORDER BY block_index, chunk_index",
            (manifest_id,),
        )
        rows = await cursor.fetchall()
        return [
            StoredChunkRef(adapter_name="sqlite", location=row[0])
            for row in rows
        ]

    async def health_check(self) -> bool:
        try:
            db = await self._ensure_db()
            cursor = await db.execute("SELECT 1")
            await cursor.fetchone()
            return True
        except Exception:
            return False
