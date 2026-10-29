"""MySQL adapter -- stores chunks as rows in a MySQL table.

Each chunk becomes a row with its encoded data in a LONGTEXT column.

Requires: pip install aiomysql
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "mysql"
_DDL = """
CREATE TABLE IF NOT EXISTS picard_chunks (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    manifest_id VARCHAR(255) NOT NULL DEFAULT '',
    chunk_key VARCHAR(64) NOT NULL,
    data LONGTEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_chunk_key (chunk_key),
    KEY idx_manifest (manifest_id)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin
"""


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class MysqlAdapter(Adapter):

    def __init__(self, *, host: str = "127.0.0.1", port: int = 3306,
                 user: str = "root", password: str = "", db: str = "picard") -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._db = db
        self._pool = None

    async def _get_pool(self):
        if self._pool is None:
            import aiomysql
            self._pool = await aiomysql.create_pool(
                host=self._host, port=self._port,
                user=self._user, password=self._password,
                db=self._db, autocommit=True, charset="utf8mb4",
            )
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(_DDL)
        return self._pool

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=10_000_000,
            persistent=True,
            ordered=False,
            tags={"requires": "aiomysql"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        pool = await self._get_pool()
        chunk_key = uuid.uuid4().hex
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO picard_chunks (manifest_id, chunk_key, data) VALUES (%s, %s, %s)",
                    (manifest_id, chunk_key, encoded_chunk),
                )
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"chunk_key": chunk_key}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        chunk_key = json.loads(ref.location)["chunk_key"]
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT data FROM picard_chunks WHERE chunk_key=%s", (chunk_key,)
                )
                row = await cur.fetchone()
        if not row:
            raise KeyError(f"mysql chunk not found: {chunk_key}")
        return row[0]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        chunk_key = json.loads(ref.location)["chunk_key"]
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM picard_chunks WHERE chunk_key=%s", (chunk_key,)
                )
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT chunk_key FROM picard_chunks WHERE manifest_id=%s",
                    (manifest_id,),
                )
                rows = await cur.fetchall()
        return [StoredChunkRef(adapter_name=_NAME,
                               location=json.dumps({"chunk_key": r[0]})) for r in rows]

    async def health_check(self) -> bool:
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT 1")
                    await cur.fetchone()
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None
