"""ClickHouse row adapter -- stores chunks in a ClickHouse MergeTree table.

Each write_chunk inserts a row into the picard.chunks table via the
ClickHouse HTTP interface. Reads use SELECT by token. ClickHouse's
columnar storage makes this efficient for high-volume chunk writes.

Requires: pip install aiohttp
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "clickhouse-row"
_DB = "picard_ch"
_TABLE = "chunks"

_DDL = (
    f"CREATE TABLE IF NOT EXISTS {_DB}.{_TABLE} "
    "(manifest_id String, token String, chunk String) "
    "ENGINE = MergeTree() "
    "ORDER BY (manifest_id, token)"
)


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class ClickhouseRowAdapter(Adapter):

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8123,
        username: str = "default",
        password: str = "",
    ) -> None:
        self._base_url = f"http://{host}:{port}"
        self._username = username
        self._password = password
        self._session = None
        self._initialized = False
        self._reg: dict[str, str] = {}

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            auth = aiohttp.BasicAuth(self._username, self._password) if self._password else None
            self._session = aiohttp.ClientSession(auth=auth)
        return self._session

    async def _query(self, sql: str, params: dict | None = None) -> dict:
        session = await self._get_session()
        url_params: dict = {"default_format": "JSON"}
        if params:
            url_params.update(params)
        async with session.post(
            self._base_url,
            params={"query": sql, **url_params},
        ) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def _ensure_schema(self) -> None:
        if self._initialized:
            return
        await self._query(f"CREATE DATABASE IF NOT EXISTS {_DB}")
        await self._query(_DDL)
        self._initialized = True

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=1_048_576,
            persistent=True,
            ordered=False,
            tags={"requires": "aiohttp", "substrate": "clickhouse-mergetree-row"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        await self._ensure_schema()
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)

        await self._query(
            f"INSERT INTO {_DB}.{_TABLE} VALUES "
            "({manifest_id:String}, {token:String}, {chunk:String})",
            params={"param_manifest_id": manifest_id,
                    "param_token": token,
                    "param_chunk": encoded_chunk},
        )

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

        await self._ensure_schema()
        result = await self._query(
            f"SELECT chunk FROM {_DB}.{_TABLE} WHERE token={{token:String}} LIMIT 1",
            params={"param_token": token},
        )
        rows = result.get("data", [])
        if not rows:
            raise KeyError(f"clickhouse-row chunk not found: {token}")
        chunk = rows[0]["chunk"]
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        token = loc["token"]
        manifest_id = loc.get("manifest", "")
        self._reg.pop(token, None)
        try:
            await self._query(
                f"ALTER TABLE {_DB}.{_TABLE} DELETE "
                "WHERE token={token:String} AND manifest_id={manifest_id:String}",
                params={"param_token": token, "param_manifest_id": manifest_id},
            )
        except Exception:
            pass
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
            await self._ensure_schema()
            result = await self._query(
                f"SELECT token FROM {_DB}.{_TABLE} WHERE manifest_id={{manifest_id:String}}",
                params={"param_manifest_id": manifest_id},
            )
            tokens_seen = {json.loads(r.location)["token"] for r in refs}
            for row in result.get("data", []):
                tok = row["token"]
                if tok not in tokens_seen:
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
            async with session.get(f"{self._base_url}/ping") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
