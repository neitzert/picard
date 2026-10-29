"""CockroachDB row adapter -- stores chunks in a CockroachDB table.

CockroachDB is PostgreSQL-compatible. Connects via asyncpg. Creates a
picard_chunks table on first use. Each write_chunk inserts a row; cold
reads SELECT by token.

Requires: pip install asyncpg
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "cockroachdb-row"
_TABLE = "picard_chunks"


def _manifest_id(chunk: str) -> str:
    try:
        parts = chunk.split(":")
        return parts[2] if parts[0] == "PCHK" and len(parts) >= 3 else ""
    except Exception:
        return ""


class CockroachdbRowAdapter(Adapter):

    def __init__(self, *, host: str = "", port: int = 26257,
                 user: str = "root", password: str = "",
                 database: str = "defaultdb",
                 ssl: bool = True) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._database = database
        self._ssl = ssl
        self._pool = None
        self._reg: dict[str, str] = {}

    async def _get_pool(self):
        if self._pool is None:
            import asyncpg
            import ssl as ssl_mod
            ssl_ctx = ssl_mod.create_default_context() if self._ssl else None
            dsn = (f"postgresql://{self._user}:{self._password}"
                   f"@{self._host}:{self._port}/{self._database}")
            self._pool = await asyncpg.create_pool(
                dsn, min_size=1, max_size=5, ssl=ssl_ctx,
            )
            async with self._pool.acquire() as conn:
                await conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
                    f"  token STRING PRIMARY KEY,"
                    f"  chunk STRING NOT NULL,"
                    f"  manifest STRING"
                    f")"
                )
        return self._pool

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            tags={"requires": "asyncpg", "substrate": "cockroachdb-row"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        if not self._host:
            raise RuntimeError("cockroachdb-row requires host to be configured")
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id(encoded_chunk)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"INSERT INTO {_TABLE} (token, chunk, manifest) VALUES ($1, $2, $3)",
                token, encoded_chunk, manifest_id,
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
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT chunk FROM {_TABLE} WHERE token = $1", token
            )
        if not row:
            raise KeyError(f"cockroachdb-row chunk not found: {token}")
        chunk = row["chunk"]
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"DELETE FROM {_TABLE} WHERE token = $1", loc["token"]
            )
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT token FROM {_TABLE} WHERE manifest = $1", manifest_id
            )
        return [
            StoredChunkRef(
                adapter_name=_NAME,
                location=json.dumps({"token": r["token"], "manifest": manifest_id}),
            )
            for r in rows
        ]

    async def health_check(self) -> bool:
        if not self._host:
            return False
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
