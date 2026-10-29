"""MySQL user-comment adapter -- stores chunks in MySQL user COMMENT fields.

Each write_chunk creates a MySQL user account whose COMMENT stores the
encoded chunk. Cold reads query information_schema.USER_ATTRIBUTES.

Requires: pip install aiomysql
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "mysql_user_comment"
_USER_RE = re.compile(r"^picard_[0-9a-f]{16}$")


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class MysqlUserCommentAdapter(Adapter):

    def __init__(self, *, host: str = "127.0.0.1", port: int = 3306,
                 user: str = "root", password: str = "") -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._pool = None

    async def _get_pool(self):
        if self._pool is None:
            import aiomysql
            self._pool = await aiomysql.create_pool(
                host=self._host, port=self._port,
                user=self._user, password=self._password,
                db="mysql", autocommit=True, charset="utf8mb4",
            )
        return self._pool

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65535,
            persistent=True,
            ordered=False,
            tags={"requires": "aiomysql", "substrate": "mysql-user-comment"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        username = f"picard_{token}"
        escaped = encoded_chunk.replace("\\", "\\\\").replace("'", "\\'")
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"CREATE USER '{username}'@'%' COMMENT '{escaped}'"
                )
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = json.loads(ref.location)["token"]
        username = f"picard_{token}"
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT ATTRIBUTE FROM information_schema.USER_ATTRIBUTES "
                    "WHERE USER=%s AND HOST='%%'",
                    (username,),
                )
                row = await cur.fetchone()
        if not row or row[0] is None:
            raise KeyError(f"mysql_user_comment chunk not found: {token}")
        attr = json.loads(row[0])
        return attr["comment"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = json.loads(ref.location)["token"]
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(f"DROP USER IF EXISTS 'picard_{token}'@'%'")
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT USER, ATTRIBUTE FROM information_schema.USER_ATTRIBUTES "
                    "WHERE USER LIKE 'picard_%%' AND HOST='%%'"
                )
                rows = await cur.fetchall()
        refs = []
        for username, attr_json in rows:
            if not _USER_RE.match(username):
                continue
            try:
                attr = json.loads(attr_json or "{}")
                chunk = attr.get("comment", "")
            except Exception:
                continue
            if _manifest_id_from_chunk(chunk) == manifest_id:
                token = username[len("picard_"):]
                refs.append(StoredChunkRef(adapter_name=_NAME,
                                           location=json.dumps({"token": token})))
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
