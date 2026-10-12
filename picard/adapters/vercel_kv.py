"""Vercel KV adapter — stores chunks in Vercel's edge Redis store.

Vercel KV is a managed Redis-compatible KV backed by Upstash, accessible
via a simple REST API.  Commonly used by Next.js apps — chunk traffic
blends with normal app data.

Requires: pip install picard-store[vercel]  (aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class VercelKvAdapter(Adapter):
    """Scatter chunks into Vercel KV (Upstash Redis)."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        prefix: str = "picard:",
        max_chunk_bytes: int = 1_048_576,
    ) -> None:
        self._url = url.rstrip("/")
        self._token = token
        self._prefix = prefix
        self._max = max_chunk_bytes
        self._session = None


    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _get_session(self):
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="vercel-kv",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=1000,
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _chunk_key(self, encoded_chunk: str) -> str:
        try:
            chunk = Chunk.decode(encoded_chunk)
            return f"{self._prefix}{chunk.manifest_id}:b{chunk.block_index}_c{chunk.chunk_index}"
        except ValueError:
            return f"{self._prefix}{uuid.uuid4().hex}"

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        key = self._chunk_key(encoded_chunk)
        url = f"{self._url}/set/{key}"
        session = await self._get_session()
        async with session.post(
            url,
            json={"value": encoded_chunk},
            headers=self._headers(),
        ) as resp:
            resp.raise_for_status()
        return StoredChunkRef(
            adapter_name="vercel-kv", location=f"vercelkv://{key}"
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        key = ref.location.removeprefix("vercelkv://")
        url = f"{self._url}/get/{key}"
        session = await self._get_session()
        async with session.get(url, headers=self._headers()) as resp:
            resp.raise_for_status()
            data = await resp.json()
            result = data.get("result")
            if result is None:
                raise KeyError(f"key not found: {key}")
            return result

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        key = ref.location.removeprefix("vercelkv://")
        url = f"{self._url}/del/{key}"
        try:
            session = await self._get_session()
            async with session.post(url, headers=self._headers()) as resp:
                data = await resp.json()
                return data.get("result", 0) > 0
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        pattern = f"{self._prefix}{manifest_id}:*"
        url = f"{self._url}/keys/{pattern}"
        session = await self._get_session()
        async with session.get(url, headers=self._headers()) as resp:
            resp.raise_for_status()
            data = await resp.json()
        return [
            StoredChunkRef(adapter_name="vercel-kv", location=f"vercelkv://{k}")
            for k in data.get("result", [])
        ]

    async def health_check(self) -> bool:
        url = f"{self._url}/ping"
        try:
            session = await self._get_session()
            async with session.get(url, headers=self._headers()) as resp:
                data = await resp.json()
                return data.get("result") == "PONG"
        except Exception:
            return False
