"""Airtable record adapter -- stores chunks as Airtable table records.

Each write_chunk creates a record with Token, Chunk, and Manifest fields.
Cold reads fetch the record by ID via the Airtable REST API.

Requires: aiohttp, an Airtable API key and base ID.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "airtable-record"
_BASE_URL = "https://api.airtable.com/v0"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class AirtableRecordAdapter(Adapter):

    def __init__(self, *, api_key: str = "", base_id: str = "",
                 table_name: str = "chunks") -> None:
        self._api_key = api_key
        self._base_id = base_id
        self._table_name = table_name
        self._reg: dict[str, str] = {}
        self._session = None

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            headers = {}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    def _table_url(self) -> str:
        import urllib.parse
        table = urllib.parse.quote(self._table_name, safe="")
        return f"{_BASE_URL}/{self._base_id}/{table}"

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=100_000,
            persistent=True,
            ordered=False,
            rate_limit_per_min=50,
            tags={"requires": "aiohttp,airtable-api-key", "substrate": "airtable-record"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        payload = {
            "fields": {
                "Token": token,
                "Chunk": encoded_chunk,
                "Manifest": manifest_id,
            }
        }
        async with session.post(self._table_url(), json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
        record_id = data["id"]
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "record_id": record_id,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        async with session.get(
                f"{self._table_url()}/{loc['record_id']}") as resp:
            resp.raise_for_status()
            data = await resp.json()
        chunk = data["fields"]["Chunk"]
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(
                f"{self._table_url()}/{loc['record_id']}") as resp:
            resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        session = await self._get_session()
        import urllib.parse
        safe_id = manifest_id.replace("\\", "\\\\").replace("'", "\\'")
        params = urllib.parse.urlencode({
            "filterByFormula": f"{{Manifest}}='{safe_id}'"
        })
        async with session.get(
                f"{self._table_url()}?{params}") as resp:
            resp.raise_for_status()
            data = await resp.json()
        refs = []
        for rec in data.get("records", []):
            token = rec["fields"].get("Token", "")
            refs.append(StoredChunkRef(
                adapter_name=_NAME,
                location=json.dumps({"token": token, "record_id": rec["id"],
                                      "manifest": manifest_id}),
            ))
        return refs

    async def health_check(self) -> bool:
        if not self._api_key or not self._base_id:
            return False
        try:
            session = await self._get_session()
            async with session.get(
                    f"{self._table_url()}?maxRecords=1") as resp:
                return resp.status in (200, 422)
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
