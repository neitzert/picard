"""CockroachDB row adapter -- stores chunks in a CockroachDB table.

CockroachDB exposes a PostgreSQL-compatible wire protocol, so this adapter
uses asyncpg. The table uses BYTES primary key to leverage CockroachDB's
distributed range splits on token values.

Requires: pip install asyncpg
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "cockroachdb-row"
_TABLE = "picard_chunks"

_DDL = (
    f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
    "manifest_id STRING NOT NULL, "
    "token STRING NOT NULL, "
    "chunk STRING NOT NULL, "
    "PRIMARY KEY (manifest_id, token)"
    ")"
)


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class CockroachdbRowAdapter(Adapter):

    def __init__(
        self,
        *,
        dsn: str = "postgresql://root@127.0.0.1:26257/defaultdb?sslmode=disable",
    ) -> None:
        self._dsn = dsn
        self._pool = None
        self._reg: dict[str, str] = {}

    async def _get_pool(self):
        if self._pool is None:
            import asyncpg
            self._pool = await asyncpg.create_pool(self._dsn)
            async with self._pool.acquire() as conn:
                await conn.execute(_DDL)
        return self._pool

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=1_048_576,
            persistent=True,
            ordered=False,
            tags={"requires": "asyncpg", "substrate": "cockroachdb-distributed-row"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"UPSERT INTO {_TABLE} (manifest_id, token, chunk) VALUES ($1, $2, $3)",
                manifest_id, token, encoded_chunk,
            )
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]
        manifest_id = loc.get("manifest", "")
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT chunk FROM {_TABLE} WHERE manifest_id=$1 AND token=$2",
                manifest_id, token,
            )
        if row is None:
            raise KeyError(f"cockroachdb-row chunk not found: {token}")
        chunk = row["chunk"]
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        token = loc["token"]
        manifest_id = loc.get("manifest", "")
        self._reg.pop(token, None)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"DELETE FROM {_TABLE} WHERE manifest_id=$1 AND token=$2",
                manifest_id, token,
            )
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT token FROM {_TABLE} WHERE manifest_id=$1",
                manifest_id,
            )
        return [
            StoredChunkRef(
                adapter_name=_NAME,
                location=json.dumps({"token": r["token"], "manifest": manifest_id}),
            )
            for r in rows
        ]

    async def health_check(self) -> bool:
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
