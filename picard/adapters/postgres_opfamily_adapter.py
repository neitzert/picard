"""PostgreSQL operator family adapter -- stores chunks as COMMENT ON OPERATOR FAMILY.

Each write_chunk creates an operator family picard_{token} USING btree and
sets a COMMENT on it containing the encoded chunk. Cold reads query
obj_description() from pg_opfamily.

Requires: pip install asyncpg
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "postgres-opfamily"


def _manifest_id(chunk: str) -> str:
    try:
        parts = chunk.split(":")
        return parts[2] if parts[0] == "PCHK" and len(parts) >= 3 else ""
    except Exception:
        return ""


class PostgresOpfamilyAdapter(Adapter):

    def __init__(self, *, host: str = "127.0.0.1", port: int = 5432,
                 user: str = "postgres", password: str = "",
                 database: str = "picard") -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._database = database
        self._pool = None
        self._reg: dict[str, str] = {}

    async def _get_pool(self):
        if self._pool is None:
            import asyncpg
            dsn = (f"postgresql://{self._user}:{self._password}"
                   f"@{self._host}:{self._port}/{self._database}")
            self._pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
        return self._pool

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            tags={"requires": "asyncpg", "substrate": "postgres-operator-family"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id(encoded_chunk)
        name = f"picard_{token}"
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"CREATE OPERATOR FAMILY {name} USING btree"
            )
            await conn.execute(
                f"COMMENT ON OPERATOR FAMILY {name} USING btree IS $1",
                encoded_chunk,
            )
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "name": name,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT obj_description(oid, 'pg_opfamily') AS comment "
                "FROM pg_opfamily WHERE opfname = $1",
                loc["name"],
            )
        if not row or row["comment"] is None:
            raise KeyError(f"postgres-opfamily chunk not found: {token}")
        chunk = row["comment"]
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"DROP OPERATOR FAMILY IF EXISTS {loc['name']} USING btree"
            )
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT opfname, obj_description(oid, 'pg_opfamily') AS comment "
                "FROM pg_opfamily WHERE opfname LIKE 'picard_%'"
            )
        refs = []
        for row in rows:
            comment = row["comment"] or ""
            if _manifest_id(comment) == manifest_id:
                name = row["opfname"]
                token = name[len("picard_"):]
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token, "name": name,
                                          "manifest": manifest_id}),
                ))
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
