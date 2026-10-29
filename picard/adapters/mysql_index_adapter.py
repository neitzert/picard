"""MySQL index-comment adapter -- stores chunks in MySQL index COMMENTs.

Each write_chunk creates a table picard_idx_{token} with a single-column
index whose COMMENT stores the encoded chunk. Cold reads use
INFORMATION_SCHEMA.STATISTICS.

Requires: pip install aiomysql
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "mysql-index"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class MysqlIndexAdapter(Adapter):

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
            max_chunk_bytes=65535,
            persistent=True,
            ordered=False,
            tags={"requires": "aiomysql", "substrate": "mysql-index-comment"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        table = f"picard_idx_{token}"
        idx = f"idx_{token}"
        escaped = encoded_chunk.replace("'", "\\'")
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"CREATE TABLE `{table}` (id INT PRIMARY KEY)"
                )
                await cur.execute(
                    f"CREATE INDEX `{idx}` ON `{table}` (id) "
                    f"COMMENT '{escaped}'"
                )
        self._reg[token] = encoded_chunk
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token, "table": table, "idx": idx}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT INDEX_COMMENT FROM INFORMATION_SCHEMA.STATISTICS "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND INDEX_NAME=%s",
                    (loc["table"], loc["idx"]),
                )
                row = await cur.fetchone()
        if not row or not row[0]:
            raise KeyError(f"mysql_index chunk not found: {token}")
        self._reg[token] = row[0]
        return row[0]

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
                    "SELECT TABLE_NAME, INDEX_NAME, INDEX_COMMENT "
                    "FROM INFORMATION_SCHEMA.STATISTICS "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME LIKE 'picard_idx_%'"
                )
                rows = await cur.fetchall()
        refs = []
        for table_name, idx_name, comment in rows:
            if not comment:
                continue
            if _manifest_id_from_chunk(comment) == manifest_id:
                token = table_name[len("picard_idx_"):]
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token, "table": table_name, "idx": idx_name}),
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
        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None
