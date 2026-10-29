"""MySQL column-default adapter -- stores chunks as a VARCHAR column DEFAULT value.

Each write_chunk creates a table picard_cd_{token} with a single column
whose DEFAULT value is the encoded chunk. Cold reads query COLUMN_DEFAULT
from INFORMATION_SCHEMA.COLUMNS.

Requires: pip install aiomysql
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "mysql-coldefault"


def _manifest_id(chunk: str) -> str:
    try:
        parts = chunk.split(":")
        return parts[2] if parts[0] == "PCHK" and len(parts) >= 3 else ""
    except Exception:
        return ""


class MysqlColdefaultAdapter(Adapter):

    def __init__(self, *, host: str = "127.0.0.1", port: int = 3306,
                 user: str = "root", password: str = "",
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
            import aiomysql
            self._pool = await aiomysql.create_pool(
                host=self._host, port=self._port,
                user=self._user, password=self._password,
                db=self._db, autocommit=True,
            )
        return self._pool

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            tags={"requires": "aiomysql", "substrate": "mysql-column-default"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id(encoded_chunk)
        table = f"picard_cd_{token}"
        escaped = encoded_chunk.replace("\\", "\\\\").replace("'", "\\'")
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"CREATE TABLE `{table}` "
                    f"(v VARCHAR(65536) DEFAULT '{escaped}')"
                )
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "table": table,
                                  "manifest": manifest_id}),
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
                    "SELECT COLUMN_DEFAULT FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME='v'",
                    (self._db, loc["table"]),
                )
                row = await cur.fetchone()
        if not row or row[0] is None:
            raise KeyError(f"mysql-coldefault chunk not found: {token}")
        chunk = row[0]
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(f"DROP TABLE IF EXISTS `{loc['table']}`")
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT TABLE_NAME, COLUMN_DEFAULT FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA=%s AND TABLE_NAME LIKE 'picard_cd_%%' "
                    "AND COLUMN_NAME='v'",
                    (self._db,),
                )
                rows = await cur.fetchall()
        refs = []
        for (table, default) in rows:
            if not default:
                continue
            if _manifest_id(default) == manifest_id:
                token = table[len("picard_cd_"):]
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token, "table": table,
                                          "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
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
