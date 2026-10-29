"""PostgreSQL schema-comment adapter -- stores chunks in PostgreSQL schema COMMENTs.

Each write_chunk creates a named schema whose COMMENT stores the encoded chunk.
Uses the pg_namespace catalog as substrate. Schemas are lightweight objects.

Requires: pip install asyncpg
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "postgres_schema"
_SCH_RE = re.compile(r"^picard_[0-9a-f]{16}$")


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class PostgresSchemaAdapter(Adapter):

    def __init__(self, *, host: str = "127.0.0.1", port: int = 5432,
                 user: str = "postgres", password: str = "",
                 db: str = "picard") -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._db = db
        self._pool = None

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
            tags={"requires": "asyncpg", "substrate": "postgres-schema-comment"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        sname = f"picard_{token}"
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(f"CREATE SCHEMA {sname}")
            await conn.execute(
                f"COMMENT ON SCHEMA {sname} IS $picard${encoded_chunk}$picard$"
            )
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = json.loads(ref.location)["token"]
        sname = f"picard_{token}"
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT obj_description(n.oid, 'pg_namespace') AS c "
                "FROM pg_namespace n WHERE n.nspname=$1",
                sname,
            )
        if not row or row["c"] is None:
            raise KeyError(f"postgres_schema chunk not found: {token}")
        return row["c"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = json.loads(ref.location)["token"]
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(f"DROP SCHEMA IF EXISTS picard_{token}")
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT n.nspname, obj_description(n.oid, 'pg_namespace') AS comment "
                "FROM pg_namespace n WHERE n.nspname LIKE 'picard_%'"
            )
        refs = []
        for row in rows:
            if not _SCH_RE.match(row["nspname"]):
                continue
            comment = row["comment"] or ""
            if _manifest_id_from_chunk(comment) == manifest_id:
                token = row["nspname"][len("picard_"):]
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
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
