"""RethinkDB document adapter -- stores chunks as RethinkDB documents.

Each write_chunk inserts a document into the picard table with the encoded
chunk and token. Reads use get() by the document's primary key (the token).
RethinkDB's changefeeds make this suitable for real-time chunk notification.

Requires: pip install rethinkdb
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "rethinkdb-doc"
_DB = "picard"
_TABLE = "chunks"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class RethinkdbDocAdapter(Adapter):

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 28015,
        db: str = _DB,
        username: str = "admin",
        password: str = "",
    ) -> None:
        self._host = host
        self._port = port
        self._db = db
        self._username = username
        self._password = password
        self._conn = None
        self._reg: dict[str, str] = {}

    async def _get_conn(self):
        if self._conn is None:
            from rethinkdb import r as R

            def _connect():
                conn = R.connect(
                    host=self._host, port=self._port,
                    db=self._db,
                    user=self._username, password=self._password,
                )
                try:
                    R.db_create(self._db).run(conn)
                except Exception:
                    pass
                try:
                    R.db(self._db).table_create(_TABLE).run(conn)
                except Exception:
                    pass
                return conn

            self._conn = await asyncio.to_thread(_connect)
        return self._conn

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=16_777_216,
            persistent=True,
            ordered=False,
            tags={"requires": "rethinkdb", "substrate": "rethinkdb-document"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        from rethinkdb import r as R

        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        conn = await self._get_conn()

        def _insert():
            return R.db(self._db).table(_TABLE).insert({
                "id": token,
                "chunk": encoded_chunk,
                "manifest": manifest_id,
            }).run(conn)

        await asyncio.to_thread(_insert)
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        from rethinkdb import r as R

        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]

        conn = await self._get_conn()

        def _get():
            return R.db(self._db).table(_TABLE).get(token).run(conn)

        doc = await asyncio.to_thread(_get)
        if not doc:
            raise KeyError(f"rethinkdb-doc chunk not found: {token}")
        chunk = doc["chunk"]
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        from rethinkdb import r as R

        loc = json.loads(ref.location)
        token = loc["token"]
        self._reg.pop(token, None)
        conn = await self._get_conn()

        def _delete():
            return R.db(self._db).table(_TABLE).get(token).delete().run(conn)

        await asyncio.to_thread(_delete)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        from rethinkdb import r as R

        conn = await self._get_conn()

        def _list():
            return list(
                R.db(self._db).table(_TABLE)
                .filter(R.row["manifest"].eq(manifest_id))
                .pluck("id").run(conn)
            )

        docs = await asyncio.to_thread(_list)
        return [
            StoredChunkRef(
                adapter_name=_NAME,
                location=json.dumps({"token": d["id"], "manifest": manifest_id}),
            )
            for d in docs
        ]

    async def health_check(self) -> bool:
        try:
            from rethinkdb import r as R
            conn = await self._get_conn()

            def _ping():
                return R.db_list().run(conn)

            await asyncio.to_thread(_ping)
            return True
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._conn is not None:
            await asyncio.to_thread(self._conn.close)
            self._conn = None
