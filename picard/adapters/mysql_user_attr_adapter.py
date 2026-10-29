"""MySQL user-attribute adapter -- stores chunks in MySQL user ATTRIBUTE JSON.

Each write_chunk creates a dedicated MySQL user picard_{token}@localhost and
sets its ATTRIBUTE to {"chunk": encoded_chunk, "manifest": manifest_id} via
ALTER USER ... ATTRIBUTE. Cold reads query INFORMATION_SCHEMA.USER_ATTRIBUTES.

Requires: pip install aiomysql
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "mysql-user-attr"
_USER_RE_PREFIX = "picard_"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class MysqlUserAttrAdapter(Adapter):

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
            tags={"requires": "aiomysql", "substrate": "mysql-user-attribute"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        attr = json.dumps({"chunk": encoded_chunk, "manifest": manifest_id})
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"CREATE USER IF NOT EXISTS 'picard_{token}'@'localhost'"
                )
                await cur.execute(
                    f"ALTER USER 'picard_{token}'@'localhost' ATTRIBUTE %s",
                    (attr,),
                )
        self._reg[token] = encoded_chunk
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = json.loads(ref.location)["token"]
        if token in self._reg:
            return self._reg[token]
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT ATTRIBUTE FROM INFORMATION_SCHEMA.USER_ATTRIBUTES "
                    "WHERE USER=%s AND HOST='localhost'",
                    (f"picard_{token}",),
                )
                row = await cur.fetchone()
        if not row or not row[0]:
            raise KeyError(f"mysql-user-attr chunk not found: {token}")
        chunk = json.loads(row[0])["chunk"]
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = json.loads(ref.location)["token"]
        self._reg.pop(token, None)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"DROP USER IF EXISTS 'picard_{token}'@'localhost'"
                )
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT USER, ATTRIBUTE FROM INFORMATION_SCHEMA.USER_ATTRIBUTES "
                    "WHERE USER LIKE 'picard_%' AND HOST='localhost'"
                )
                rows = await cur.fetchall()
        refs = []
        for (uname, attr_json) in rows:
            if not attr_json:
                continue
            try:
                obj = json.loads(attr_json)
                if obj.get("manifest") == manifest_id:
                    token = uname[len("picard_"):]
                    refs.append(StoredChunkRef(
                        adapter_name=_NAME,
                        location=json.dumps({"token": token}),
                    ))
            except Exception:
                continue
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
