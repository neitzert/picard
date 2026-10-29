"""ScyllaDB REST API adapter -- stores chunks via ScyllaDB's native REST API.

ScyllaDB exposes a REST/JSON API on port 10000. This adapter uses that API
directly without a CQL driver, making it usable from environments where
cassandra-driver is unavailable. Each chunk is stored as a row in the
picard keyspace.

Requires: pip install aiohttp
"""
from __future__ import annotations

import json
import urllib.parse
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "scylladb-row"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class ScylladbRowAdapter(Adapter):

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        cql_port: int = 9042,
        api_port: int = 10000,
        username: str = "",
        password: str = "",
        keyspace: str = "picard_scylla",
        table: str = "chunks",
    ) -> None:
        self._cql_url = f"http://{host}:{api_port}"
        self._host = host
        self._cql_port = cql_port
        self._username = username
        self._password = password
        self._keyspace = keyspace
        self._table = table
        self._session = None
        self._reg: dict[str, str] = {}
        self._initialized = False

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            auth = None
            if self._username:
                auth = aiohttp.BasicAuth(self._username, self._password)
            self._session = aiohttp.ClientSession(auth=auth)
        return self._session

    async def _ensure_schema(self) -> None:
        if self._initialized:
            return
        session = await self._get_session()
        ks_url = f"{self._cql_url}/storage_service/keyspaces/{self._keyspace}"
        payload = {
            "replication_factor": "1",
            "strategy_class": "SimpleStrategy",
        }
        async with session.post(ks_url, json=payload) as resp:
            pass  # 200 = created, 400 = already exists

        col_url = f"{self._cql_url}/column_family/{self._keyspace}:{self._table}"
        table_def = {
            "column_aliases": ["manifest_id", "token"],
            "column_defs": [
                {"name": "manifest_id", "validation_class": "org.apache.cassandra.db.marshal.UTF8Type"},
                {"name": "token", "validation_class": "org.apache.cassandra.db.marshal.UTF8Type"},
                {"name": "chunk", "validation_class": "org.apache.cassandra.db.marshal.UTF8Type"},
            ],
            "comparator_type": "org.apache.cassandra.db.marshal.UTF8Type",
            "key_validation_class": "org.apache.cassandra.db.marshal.UTF8Type",
        }
        async with session.post(col_url, json=table_def) as resp:
            pass  # best-effort schema creation

        self._initialized = True

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=1_048_576,
            persistent=True,
            ordered=False,
            tags={"requires": "aiohttp", "substrate": "scylladb-rest-api"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        await self._ensure_schema()
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)

        session = await self._get_session()
        url = (
            f"{self._cql_url}/column_family/rows/{self._keyspace}:{self._table}"
            f"/{urllib.parse.quote(manifest_id, safe='')}/{urllib.parse.quote(token, safe='')}"
        )
        async with session.put(url, json={"chunk": encoded_chunk}) as resp:
            if resp.status not in (200, 201, 204):
                resp.raise_for_status()

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

        manifest_id = loc.get("manifest", "")
        session = await self._get_session()
        url = (
            f"{self._cql_url}/column_family/rows/{self._keyspace}:{self._table}"
            f"/{urllib.parse.quote(manifest_id, safe='')}/{urllib.parse.quote(token, safe='')}"
        )
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()

        chunk = data.get("chunk", "")
        if not chunk:
            raise KeyError(f"scylladb-row chunk not found: {token}")
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        token = loc["token"]
        manifest_id = loc.get("manifest", "")
        self._reg.pop(token, None)

        try:
            session = await self._get_session()
            url = (
                f"{self._cql_url}/column_family/rows/{self._keyspace}:{self._table}"
                f"/{urllib.parse.quote(manifest_id, safe='')}/{urllib.parse.quote(token, safe='')}"
            )
            async with session.delete(url) as resp:
                return resp.status in (200, 204, 404)
        except Exception:
            return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token, "manifest": manifest_id}),
                ))

        try:
            session = await self._get_session()
            url = (
                f"{self._cql_url}/column_family/rows/{self._keyspace}:{self._table}"
                f"/{urllib.parse.quote(manifest_id, safe='')}"
            )
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    tokens_seen = {json.loads(r.location)["token"] for r in refs}
                    for row in (data if isinstance(data, list) else []):
                        tok = row.get("token", "")
                        if tok and tok not in tokens_seen:
                            refs.append(StoredChunkRef(
                                adapter_name=_NAME,
                                location=json.dumps({"token": tok, "manifest": manifest_id}),
                            ))
        except Exception:
            pass

        return refs

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get(f"{self._cql_url}/") as resp:
                return resp.status in (200, 404)
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
