"""MySQL trigger adapter -- stores chunks inside a MySQL trigger body.

Each write_chunk creates a scratch table if needed, then a trigger
picard_{token} that assigns the encoded chunk to a session variable
@picard_chunk. Cold reads parse the trigger body from INFORMATION_SCHEMA.

Requires: pip install aiomysql
"""
from __future__ import annotations

import base64
import json
import re
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "mysql-trigger"
_SCRATCH_TABLE = "picard_trigger_scratch"


def _manifest_id(chunk: str) -> str:
    try:
        parts = chunk.split(":")
        return parts[2] if parts[0] == "PCHK" and len(parts) >= 3 else ""
    except Exception:
        return ""


class MysqlTriggerAdapter(Adapter):

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
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"CREATE TABLE IF NOT EXISTS `{_SCRATCH_TABLE}` "
                        f"(id INT AUTO_INCREMENT PRIMARY KEY)"
                    )
        return self._pool

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            tags={"requires": "aiomysql", "substrate": "mysql-trigger-body"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id(encoded_chunk)
        trig_name = f"picard_{token}"
        b64 = base64.b64encode(encoded_chunk.encode()).decode()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"CREATE TRIGGER `{trig_name}` BEFORE INSERT "
                    f"ON `{_SCRATCH_TABLE}` FOR EACH ROW "
                    f"SET @picard_chunk = '{b64}'"
                )
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "trigger": trig_name,
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
                    "SELECT ACTION_STATEMENT FROM INFORMATION_SCHEMA.TRIGGERS "
                    "WHERE TRIGGER_SCHEMA=%s AND TRIGGER_NAME=%s",
                    (self._db, loc["trigger"]),
                )
                row = await cur.fetchone()
        if not row:
            raise KeyError(f"mysql-trigger chunk not found: {token}")
        stmt = row[0]
        m = re.search(r"@picard_chunk\s*=\s*'([A-Za-z0-9+/=]+)'", stmt)
        if not m:
            raise KeyError(f"mysql-trigger parse failed: {token}")
        chunk = base64.b64decode(m.group(1)).decode()
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(f"DROP TRIGGER IF EXISTS `{loc['trigger']}`")
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token,
                                          "trigger": f"picard_{token}",
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
