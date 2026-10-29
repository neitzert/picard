"""Elasticsearch document adapter -- stores chunks as Elasticsearch documents.

Each write_chunk indexes a document with the encoded chunk into index
picard_{manifest_id} (or a default index). Cold reads GET the document by
its generated ID.

Requires: aiohttp; a running Elasticsearch instance
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "elasticsearch-doc"


def _manifest_id(chunk: str) -> str:
    try:
        parts = chunk.split(":")
        return parts[2] if parts[0] == "PCHK" and len(parts) >= 3 else ""
    except Exception:
        return ""


class ElasticsearchDocAdapter(Adapter):

    def __init__(self, *, url: str = "http://127.0.0.1:9200",
                 index: str = "picard",
                 username: str = "", password: str = "") -> None:
        self._url = url.rstrip("/")
        self._index = index
        self._username = username
        self._password = password
        self._session = None
        self._reg: dict[str, str] = {}

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            auth = None
            if self._username:
                auth = aiohttp.BasicAuth(self._username, self._password)
            self._session = aiohttp.ClientSession(auth=auth)
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=10_000_000,
            persistent=True,
            ordered=False,
            tags={"requires": "aiohttp", "substrate": "elasticsearch-document"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id(encoded_chunk)
        doc = {"chunk": encoded_chunk, "manifest": manifest_id, "token": token}
        session = await self._get_session()
        async with session.put(
            f"{self._url}/{self._index}/_doc/{token}",
            json=doc,
        ) as resp:
            if resp.status not in (200, 201):
                text = await resp.text()
                raise RuntimeError(f"ES error {resp.status}: {text}")
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "index": self._index,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]
        session = await self._get_session()
        async with session.get(
            f"{self._url}/{loc['index']}/_doc/{token}",
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        chunk = data["_source"]["chunk"]
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(
            f"{self._url}/{loc['index']}/_doc/{loc['token']}",
        ) as resp:
            return resp.status in (200, 404)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token, "index": self._index,
                                          "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        if not self._url:
            return False
        try:
            session = await self._get_session()
            async with session.get(f"{self._url}/") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
