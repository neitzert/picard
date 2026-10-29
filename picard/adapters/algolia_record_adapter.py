"""Algolia record adapter -- stores chunks as Algolia index records.

Each write_chunk adds a record with objectID=token, chunk, and manifest.
Cold reads fetch via GET /1/indexes/{index}/{token}.

Requires: aiohttp, an Algolia application ID, API key, and index name.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "algolia-record"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class AlgoliaRecordAdapter(Adapter):

    def __init__(self, *, app_id: str = "", api_key: str = "",
                 index_name: str = "picard") -> None:
        self._app_id = app_id
        self._api_key = api_key
        self._index_name = index_name
        self._reg: dict[str, str] = {}
        self._session = None

    def _base_url(self) -> str:
        return f"https://{self._app_id}.algolia.net/1/indexes/{self._index_name}"

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            headers = {
                "X-Algolia-Application-Id": self._app_id,
                "X-Algolia-API-Key": self._api_key,
            }
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=102400,
            persistent=True,
            ordered=False,
            rate_limit_per_min=100,
            tags={"requires": "aiohttp,algolia-credentials", "substrate": "algolia-index-record"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        payload = {"objectID": token, "chunk": encoded_chunk,
                   "manifest": manifest_id}
        async with session.post(self._base_url(), json=payload) as resp:
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
                f"{self._base_url()}/{loc['token']}") as resp:
            resp.raise_for_status()
            data = await resp.json()
        chunk = data["chunk"]
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(
                f"{self._base_url()}/{loc['token']}") as resp:
            resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        session = await self._get_session()
        async with session.post(
                f"{self._base_url()}/query",
                json={"filters": f"manifest:{manifest_id}",
                      "hitsPerPage": 1000}) as resp:
            resp.raise_for_status()
            data = await resp.json()
        refs = []
        for hit in data.get("hits", []):
            token = hit.get("objectID", "")
            refs.append(StoredChunkRef(
                adapter_name=_NAME,
                location=json.dumps({"token": token, "manifest": manifest_id}),
            ))
        return refs

    async def health_check(self) -> bool:
        if not self._app_id or not self._api_key:
            return False
        try:
            session = await self._get_session()
            async with session.get(
                    f"https://{self._app_id}.algolia.net/1/indexes") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
