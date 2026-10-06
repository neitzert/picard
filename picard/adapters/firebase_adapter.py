"""Firebase Realtime Database adapter — stores chunks as JSON nodes.

Each chunk becomes a node at /{base_path}/{chunk_key}.json in the
Firebase RTDB, accessed via the REST API.

Requires: pip install picard[firebase]  (aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class FirebaseAdapter(Adapter):
    """Scatter chunks into Firebase Realtime Database nodes."""

    def __init__(
        self,
        database_url: str,
        *,
        auth_token: str | None = None,
        base_path: str = "picard/chunks",
        max_chunk_bytes: int = 10_485_760,
    ) -> None:
        self._db_url = database_url.rstrip("/")
        self._auth = auth_token
        self._base = base_path.strip("/")
        self._max = max_chunk_bytes
        self._client: object | None = None

    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        import aiohttp

        self._client = aiohttp.ClientSession()
        return self._client

    def _url(self, key: str) -> str:
        url = f"{self._db_url}/{self._base}/{key}.json"
        if self._auth:
            url += f"?auth={self._auth}"
        return url

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="firebase",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=1000,
        )

    def _chunk_key(self, encoded_chunk: str) -> str:
        try:
            chunk = Chunk.decode(encoded_chunk)
            return f"{chunk.manifest_id}_{chunk.block_index}_{chunk.chunk_index}"
        except ValueError:
            return uuid.uuid4().hex

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import json

        key = self._chunk_key(encoded_chunk)
        client = await self._ensure_client()
        async with client.put(
            self._url(key),
            data=json.dumps({"data": encoded_chunk}),
            headers={"Content-Type": "application/json"},
        ) as resp:
            resp.raise_for_status()
        loc = f"firebase://{self._base}/{key}"
        return StoredChunkRef(adapter_name="firebase", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        key = ref.location.rsplit("/", 1)[-1]
        client = await self._ensure_client()
        async with client.get(self._url(key)) as resp:
            resp.raise_for_status()
            data = await resp.json()
        if data is None:
            raise KeyError(f"chunk not found: {key}")
        return data["data"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        key = ref.location.rsplit("/", 1)[-1]
        client = await self._ensure_client()
        try:
            async with client.delete(self._url(key)) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._ensure_client()
        url = f"{self._db_url}/{self._base}.json"
        if self._auth:
            url += f"?auth={self._auth}"
        url += "&shallow=true" if "?" in url else "?shallow=true"
        try:
            async with client.get(url) as resp:
                data = await resp.json()
            if not data:
                return []
            prefix = f"{manifest_id}_"
            return [
                StoredChunkRef(
                    adapter_name="firebase",
                    location=f"firebase://{self._base}/{k}",
                )
                for k in data
                if k.startswith(prefix)
            ]
        except Exception:
            return []

    async def health_check(self) -> bool:
        try:
            client = await self._ensure_client()
            url = f"{self._db_url}/.json?shallow=true"
            if self._auth:
                url += f"&auth={self._auth}"
            async with client.get(url) as resp:
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
