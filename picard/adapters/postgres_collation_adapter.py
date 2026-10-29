"""PostgreSQL collation-comment adapter -- stores chunks in collation COMMENTs.

Each write_chunk creates a collation (picard_{token}) with LOCALE='C' and
stores the encoded chunk as its COMMENT. Collations live in pg_collation.

Requires: pip install asyncpg
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "postgres-collation"
_COLL_RE = re.compile(r"^picard_[0-9a-f]{16}$")


def _manifest_id(chunk: str) -> str:
    try:
        parts = chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class PostgresCollationAdapter(Adapter):

    def __init__(self, *, host: str = "127.0.0.1", port: int = 5432,
                 user: str = "postgres", password: str = "",
                 db: str = "picard") -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._db = db
        self._pool = None
        self._reg: dict[str, str] = {}

    async def _get_pool(self):
        if self._pool is None:
            import asyncpg
            dsn = (f"postgresql://{self._user}:{self._password}"
                   f"@{self._host}:{self._port}/{self._db}")
            self._pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
        return self._pool

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=1_000_000,
            persistent=True,
            ordered=False,
            tags={"requires": "asyncpg", "substrate": "postgres-collation-comment"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        coll_name = f"picard_{token}"
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"CREATE COLLATION {coll_name} (LOCALE = 'C')"
            )
            await conn.execute(
                f"COMMENT ON COLLATION {coll_name} IS "
                f"$picard${encoded_chunk}$picard$"
            )
        self._reg[token] = encoded_chunk
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = json.loads(ref.location)["token"]
        if token in self._reg:
            return self._reg[token]
        coll_name = f"picard_{token}"
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT obj_description(c.oid, 'pg_collation') AS c "
                "FROM pg_collation c WHERE c.collname = $1",
                coll_name,
            )
        if not row or row["c"] is None:
            raise KeyError(f"postgres-collation chunk not found: {token}")
        return row["c"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = json.loads(ref.location)["token"]
        self._reg.pop(token, None)
        coll_name = f"picard_{token}"
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(f"DROP COLLATION IF EXISTS {coll_name}")
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT c.collname, obj_description(c.oid, 'pg_collation') AS c "
                "FROM pg_collation c WHERE c.collname LIKE 'picard_%'"
            )
        refs = []
        for row in rows:
            if not _COLL_RE.match(row["collname"]):
                continue
            comment = row["c"] or ""
            if _manifest_id(comment) == manifest_id:
                token = row["collname"][len("picard_"):]
                refs.append(StoredChunkRef(adapter_name=_NAME,
                                           location=json.dumps({"token": token})))
        return refs

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
