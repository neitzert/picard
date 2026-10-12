"""Cloudflare Workers KV adapter — stores chunks in edge KV namespaces.

Data lives at 200+ Cloudflare edge locations worldwide, reachable via
the REST API.

Requires: pip install picard[cloudflare]  (aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class CloudflareKvAdapter(Adapter):
    """Scatter chunks into Cloudflare Workers KV."""

    def __init__(
        self,
        account_id: str,
        namespace_id: str,
        api_token: str,
        *,
        max_chunk_bytes: int = 26_214_400,
    ) -> None:
        self._account_id = account_id
        self._namespace_id = namespace_id
        self._api_token = api_token
        self._max = max_chunk_bytes
        self._base = (
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
            f"/storage/kv/namespaces/{namespace_id}"
        )
        self._session = None

    async def _get_session(self):
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="cloudflare-kv",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=1000,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_token}",
            "Content-Type": "application/octet-stream",
        }

    def _chunk_key(self, encoded_chunk: str) -> str:
        try:
            chunk = Chunk.decode(encoded_chunk)
            return f"picard/{chunk.manifest_id}/b{chunk.block_index}_c{chunk.chunk_index}"
        except ValueError:
            return f"picard/{uuid.uuid4().hex}"

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        key = self._chunk_key(encoded_chunk)
        url = f"{self._base}/values/{key}"
        session = await self._get_session()
        async with session.put(
            url, data=encoded_chunk.encode(), headers=self._headers()
        ) as resp:
            resp.raise_for_status()
        return StoredChunkRef(
            adapter_name="cloudflare-kv",
            location=f"cfkv://{self._namespace_id}/{key}",
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        key = ref.location.split("/", 2)[-1] if "://" in ref.location else ref.location
        url = f"{self._base}/values/{key}"
        session = await self._get_session()
        async with session.get(url, headers=self._headers()) as resp:
            resp.raise_for_status()
            return (await resp.read()).decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        key = ref.location.split("/", 2)[-1] if "://" in ref.location else ref.location
        url = f"{self._base}/values/{key}"
        try:
            session = await self._get_session()
            async with session.delete(url, headers=self._headers()) as resp:
                return resp.status < 400
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        url = f"{self._base}/keys"
        params = {"prefix": f"picard/{manifest_id}/"}
        session = await self._get_session()
        async with session.get(
            url, params=params, headers=self._headers()
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        return [
            StoredChunkRef(
                adapter_name="cloudflare-kv",
                location=f"cfkv://{self._namespace_id}/{item['name']}",
            )
            for item in data.get("result", [])
        ]

    async def health_check(self) -> bool:
        url = f"{self._base}/keys"
        params = {"limit": "1"}
        try:
            session = await self._get_session()
            async with session.get(
                url, params=params, headers=self._headers()
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        return {
            "used_bytes": None,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": None,
        }
