"""PostgreSQL adapter — stores chunks as rows in a PostgreSQL table.

Each chunk becomes a row with its encoded data in a TEXT column.

Requires: pip install picard-store[postgresql]  (asyncpg)
"""

from __future__ import annotations

import re
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_VALID_TABLE_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$")


class PostgresqlAdapter(Adapter):
    """Scatter chunks into PostgreSQL rows."""

    def __init__(
        self,
        dsn: str,
        *,
        table: str = "picard_chunks",
        max_chunk_bytes: int = 100_000_000,
    ) -> None:
        if not _VALID_TABLE_RE.match(table):
            raise ValueError(f"invalid table name: {table!r}")
        self._dsn = dsn
        self._table = table
        self._max = max_chunk_bytes
        self._pool: object | None = None

    async def _ensure_pool(self):
        if self._pool is not None:
            return self._pool
        import asyncpg

        self._pool = await asyncpg.create_pool(self._dsn)
        async with self._pool.acquire() as conn:
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self._table} (
                    id TEXT PRIMARY KEY,
                    manifest_id TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        return self._pool

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="postgresql",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=5000,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        pool = await self._ensure_pool()
        chunk = Chunk.decode(encoded_chunk)
        row_id = uuid.uuid4().hex
        async with pool.acquire() as conn:
            await conn.execute(
                f"INSERT INTO {self._table} (id, manifest_id, data) "
                f"VALUES ($1, $2, $3)",
                row_id,
                chunk.manifest_id,
                encoded_chunk,
            )
        return StoredChunkRef(adapter_name="postgresql", location=row_id)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT data FROM {self._table} WHERE id = $1",
                ref.location,
            )
        if row is None:
            raise ValueError(f"chunk not found: {ref.location}")
        return row["data"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        pool = await self._ensure_pool()
        try:
            async with pool.acquire() as conn:
                result = await conn.execute(
                    f"DELETE FROM {self._table} WHERE id = $1",
                    ref.location,
                )
                return result == "DELETE 1"
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT id FROM {self._table} WHERE manifest_id = $1",
                manifest_id,
            )
        return [
            StoredChunkRef(adapter_name="postgresql", location=row["id"])
            for row in rows
        ]

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def health_check(self) -> bool:
        try:
            pool = await self._ensure_pool()
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return True
        except Exception:
            return False
