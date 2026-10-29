"""Supabase row adapter -- stores chunks as rows in a Supabase/PostgREST table.

Each write_chunk inserts a row into a 'picard_chunks' table with columns
token, chunk, and manifest. Cold reads query by token.

Requires: aiohttp, a Supabase project URL and service role key.
Table must exist: CREATE TABLE picard_chunks (token text primary key, chunk text, manifest text);
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "supabase-row"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class SupabaseRowAdapter(Adapter):

    def __init__(self, *, project_url: str = "", service_key: str = "",
                 table: str = "picard_chunks") -> None:
        self._project_url = project_url.rstrip("/")
        self._service_key = service_key
        self._table = table
        self._reg: dict[str, str] = {}
        self._session = None

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            headers = {
                "apikey": self._service_key,
                "Authorization": f"Bearer {self._service_key}",
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            }
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    def _rest_url(self) -> str:
        return f"{self._project_url}/rest/v1/{self._table}"

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=1_000_000,
            persistent=True,
            ordered=False,
            rate_limit_per_min=500,
            tags={"requires": "aiohttp,supabase-service-key", "substrate": "supabase-postgrest-row"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        payload = {"token": token, "chunk": encoded_chunk,
                   "manifest": manifest_id}
        async with session.post(self._rest_url(), json=payload) as resp:
            resp.raise_for_status()
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        async with session.get(
                self._rest_url(),
                params={"token": f"eq.{loc['token']}"}) as resp:
            resp.raise_for_status()
            rows = await resp.json()
        chunk = rows[0]["chunk"]
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(
                self._rest_url(),
                params={"token": f"eq.{loc['token']}"}) as resp:
            resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        session = await self._get_session()
        async with session.get(
                self._rest_url(),
                params={"manifest": f"eq.{manifest_id}"}) as resp:
            resp.raise_for_status()
            rows = await resp.json()
        return [
            StoredChunkRef(
                adapter_name=_NAME,
                location=json.dumps({"token": row["token"],
                                      "manifest": manifest_id}),
            )
            for row in rows
        ]

    async def health_check(self) -> bool:
        if not self._project_url or not self._service_key:
            return False
        try:
            session = await self._get_session()
            async with session.get(
                    f"{self._project_url}/rest/v1/",
                    params={"limit": "0"}) as resp:
                return resp.status in (200, 404)
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
