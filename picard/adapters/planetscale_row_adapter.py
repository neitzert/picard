"""PlanetScale row adapter -- stores chunks in a PlanetScale (MySQL-compatible) table.

Connects to PlanetScale via standard MySQL protocol using aiomysql. Creates
a chunks table on first use. Each write_chunk inserts a row; cold reads
SELECT by token.

Requires: pip install aiomysql
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "planetscale-row"
_TABLE = "picard_chunks"


def _manifest_id(chunk: str) -> str:
    try:
        parts = chunk.split(":")
        return parts[2] if parts[0] == "PCHK" and len(parts) >= 3 else ""
    except Exception:
        return ""


class PlanetscaleRowAdapter(Adapter):

    def __init__(self, *, host: str = "", port: int = 3306,
                 user: str = "", password: str = "",
                 database: str = "picard",
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
            import aiomysql
            ssl_ctx = True if self._ssl else None
            self._pool = await aiomysql.create_pool(
                host=self._host, port=self._port,
                user=self._user, password=self._password,
                db=self._database, autocommit=True,
                ssl=ssl_ctx,
            )
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"CREATE TABLE IF NOT EXISTS `{_TABLE}` ("
                        f"  token VARCHAR(32) PRIMARY KEY,"
                        f"  chunk LONGTEXT NOT NULL,"
                        f"  manifest VARCHAR(128)"
                        f")"
                    )
        return self._pool

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            tags={"requires": "aiomysql", "substrate": "planetscale-mysql-row"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        if not self._host:
            raise RuntimeError("planetscale-row requires host to be configured")
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id(encoded_chunk)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"INSERT INTO `{_TABLE}` (token, chunk, manifest) "
                    f"VALUES (%s, %s, %s)",
                    (token, encoded_chunk, manifest_id),
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
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT chunk FROM `{_TABLE}` WHERE token=%s", (token,)
                )
                row = await cur.fetchone()
        if not row:
            raise KeyError(f"planetscale-row chunk not found: {token}")
        chunk = row[0]
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"DELETE FROM `{_TABLE}` WHERE token=%s", (loc["token"],)
                )
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT token FROM `{_TABLE}` WHERE manifest=%s",
                    (manifest_id,),
                )
                rows = await cur.fetchall()
        return [
            StoredChunkRef(
                adapter_name=_NAME,
                location=json.dumps({"token": r[0], "manifest": manifest_id}),
            )
            for r in rows
        ]

    async def health_check(self) -> bool:
        if not self._host:
            return False
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT 1")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None
