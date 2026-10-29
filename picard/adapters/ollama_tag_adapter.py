"""Ollama tag adapter -- stores chunks as Ollama blob objects.

Each write_chunk pushes the encoded chunk as a raw blob via POST
/api/blobs/sha256:{sha256(content)}. The blob persists in Ollama's blob
store. Presence is verifiable via HEAD; the chunk is also stored in the
location JSON for reliable cold reads (Ollama blobs are content-addressed
write-once stores with no GET endpoint).

Requires: a running Ollama instance (any version with blob API support).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "ollama-tag"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


def _blob_digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class OllamaTagAdapter(Adapter):

    def __init__(self, *, url: str = "http://127.0.0.1:11434") -> None:
        self._url = url.rstrip("/")
        self._reg: dict[str, str] = {}
        self._session = None

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            tags={"requires": "ollama", "substrate": "ollama-blob"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        data = encoded_chunk.encode("utf-8")
        digest = _blob_digest(data)
        session = await self._get_session()
        async with session.post(
            f"{self._url}/api/blobs/sha256:{digest}",
            data=data,
            headers={"Content-Type": "application/octet-stream"},
        ) as resp:
            resp.raise_for_status()
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "digest": digest,
                                  "chunk": encoded_chunk,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]
        # Cold read: chunk is stored in location (blob GET not supported)
        chunk = loc.get("chunk", "")
        if chunk:
            self._reg[token] = chunk
            return chunk
        raise KeyError(f"ollama-tag chunk not found: {token}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        # Ollama blob DELETE is not supported (405) -- blobs are GC'd automatically
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                data = chunk.encode("utf-8")
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token,
                                          "digest": _blob_digest(data),
                                          "chunk": chunk,
                                          "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get(f"{self._url}/api/version") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
