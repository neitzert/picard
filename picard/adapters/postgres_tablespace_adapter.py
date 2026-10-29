"""PostgreSQL event-trigger comment adapter -- stores chunks in event trigger COMMENTs.

Each write_chunk creates an event trigger (picard_{token}) with a stub handler
function and stores the encoded chunk as its COMMENT. Event triggers live in
pg_event_trigger.

Requires: pip install asyncpg
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "postgres-tablespace"
_EVT_RE = re.compile(r"^picard_[0-9a-f]{16}$")
_HANDLER_FN = "picard_evt_noop"


def _manifest_id(chunk: str) -> str:
    try:
        parts = chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class PostgresTablespaceAdapter(Adapter):
    """Stores chunks as COMMENTs on PostgreSQL event triggers."""

    def __init__(self, *, host: str = "127.0.0.1", port: int = 5432,
                 user: str = "postgres", password: str = "",
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
            import asyncpg
            dsn = (f"postgresql://{self._user}:{self._password}"
                   f"@{self._host}:{self._port}/{self._db}")
            self._pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
            async with self._pool.acquire() as conn:
                await conn.execute(
                    f"CREATE OR REPLACE FUNCTION {_HANDLER_FN}() "
                    f"RETURNS event_trigger LANGUAGE plpgsql AS "
                    f"$$BEGIN END;$$"
                )
        return self._pool

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=1_000_000,
            persistent=True,
            ordered=False,
            tags={"requires": "asyncpg", "substrate": "postgres-event-trigger-comment"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        evt_name = f"picard_{token}"
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"CREATE EVENT TRIGGER {evt_name} ON ddl_command_end "
                f"EXECUTE FUNCTION {_HANDLER_FN}()"
            )
            await conn.execute(
                f"COMMENT ON EVENT TRIGGER {evt_name} IS "
                f"$picard${encoded_chunk}$picard$"
            )
        self._reg[token] = encoded_chunk
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = json.loads(ref.location)["token"]
        if token in self._reg:
            return self._reg[token]
        evt_name = f"picard_{token}"
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT obj_description(oid, 'pg_event_trigger') AS c "
                "FROM pg_event_trigger WHERE evtname = $1",
                evt_name,
            )
        if not row or row["c"] is None:
            raise KeyError(f"postgres-tablespace chunk not found: {token}")
        return row["c"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = json.loads(ref.location)["token"]
        self._reg.pop(token, None)
        evt_name = f"picard_{token}"
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(f"DROP EVENT TRIGGER IF EXISTS {evt_name}")
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT evtname, obj_description(oid, 'pg_event_trigger') AS c "
                "FROM pg_event_trigger WHERE evtname LIKE 'picard_%'"
            )
        refs = []
        for row in rows:
            if not _EVT_RE.match(row["evtname"]):
                continue
            comment = row["c"] or ""
            if _manifest_id(comment) == manifest_id:
                token = row["evtname"][len("picard_"):]
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
        self._reg.clear()
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
