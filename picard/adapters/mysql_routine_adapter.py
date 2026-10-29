"""MySQL stored routine adapter -- stores chunks in MySQL procedure COMMENTs.

Each write_chunk creates a stored procedure whose COMMENT stores the encoded
chunk. Cold reads query information_schema.ROUTINES.

Requires: pip install aiomysql
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "mysql_routine"
_PROC_RE = re.compile(r"^picard_[0-9a-f]{16}$")


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class MysqlRoutineAdapter(Adapter):

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
        return self._pool

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65535,
            persistent=True,
            ordered=False,
            tags={"requires": "aiomysql", "substrate": "mysql-procedure-comment"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        proc_name = f"picard_{token}"
        escaped = encoded_chunk.replace("\\", "\\\\").replace("'", "\\'")
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"CREATE PROCEDURE `{proc_name}`() "
                    f"COMMENT '{escaped}' "
                    f"BEGIN SELECT 1; END"
                )
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token, "db": self._db}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        proc_name = f"picard_{token}"
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT ROUTINE_COMMENT FROM information_schema.ROUTINES "
                    "WHERE ROUTINE_SCHEMA=%s AND ROUTINE_NAME=%s",
                    (self._db, proc_name),
                )
                row = await cur.fetchone()
        if not row:
            raise KeyError(f"mysql_routine chunk not found: {token}")
        return row[0]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = json.loads(ref.location)["token"]
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(f"DROP PROCEDURE IF EXISTS `picard_{token}`")
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT ROUTINE_NAME, ROUTINE_COMMENT FROM information_schema.ROUTINES "
                    "WHERE ROUTINE_SCHEMA=%s AND ROUTINE_NAME LIKE 'picard_%'",
                    (self._db,),
                )
                rows = await cur.fetchall()
        refs = []
        for rname, comment in rows:
            if not _PROC_RE.match(rname):
                continue
            if _manifest_id_from_chunk(comment) == manifest_id:
                token = rname[len("picard_"):]
                refs.append(StoredChunkRef(adapter_name=_NAME,
                                           location=json.dumps({"token": token,
                                            "db": self._db})))
        return refs

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
