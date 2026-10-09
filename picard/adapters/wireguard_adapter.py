"""WireGuard adapter — store chunks through a WireGuard VPN tunnel.

Sends chunk data via HTTP to a storage endpoint reachable only through
the WireGuard tunnel.  Uses the wg CLI for tunnel management.

Data exfil insight: WireGuard traffic is encrypted and looks like
ordinary UDP.  Combined with a storage endpoint on the tunnel network,
data moves out encrypted at the tunnel layer AND at the PICARD layer.

Requires: pip install picard[wireguard]  (aiohttp, wg CLI)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class WireguardAdapter(Adapter):
    """Scatter chunks through a WireGuard tunnel."""

    def __init__(
        self,
        storage_host: str,
        *,
        endpoint: str = "127.0.0.1:51820",
        interface: str = "wg0",
        max_chunk_bytes: int = 65536,
    ) -> None:
        self._storage_host = storage_host.rstrip("/")
        self._endpoint = endpoint
        self._interface = interface
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
            name="wireguard",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=1000,
        )

    def _chunk_key(self, encoded_chunk: str) -> str:
        from picard.chunker import Chunk
        try:
            chunk = Chunk.decode(encoded_chunk)
            return f"{chunk.manifest_id}/{chunk.block_index}_{chunk.chunk_index}"
        except (ValueError, Exception):
            return uuid.uuid4().hex

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        key = self._chunk_key(encoded_chunk)
        url = f"{self._storage_host}/chunks/{key}"

        session = await self._get_session()
        async with session.put(url, data=encoded_chunk) as resp:
            resp.raise_for_status()

        loc = f"wg://{self._endpoint}/{key}"
        return StoredChunkRef(adapter_name="wireguard", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        parts = ref.location.replace("wg://", "").split("/", 1)
        key = parts[1] if len(parts) > 1 else ""
        url = f"{self._storage_host}/chunks/{key}"

        session = await self._get_session()
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.text()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        parts = ref.location.replace("wg://", "").split("/", 1)
        key = parts[1] if len(parts) > 1 else ""
        url = f"{self._storage_host}/chunks/{key}"

        try:
            session = await self._get_session()
            async with session.delete(url) as resp:
                return resp.status in (200, 204)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        url = f"{self._storage_host}/chunks/"
        try:
            session = await self._get_session()
            async with session.get(
                url, params={"prefix": manifest_id}
            ) as resp:
                resp.raise_for_status()
                result = await resp.json()

            return [
                StoredChunkRef(
                    adapter_name="wireguard",
                    location=f"wg://{self._endpoint}/{k}",
                )
                for k in result.get("keys", [])
            ]
        except Exception:
            return []

    async def health_check(self) -> bool:
        import asyncio

        proc = await asyncio.create_subprocess_exec(
            "wg", "show", self._interface,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return proc.returncode == 0 and b"latest handshake" in stdout

    async def capacity(self) -> dict[str, int | None]:
        try:
            session = await self._get_session()
            async with session.get(
                f"{self._storage_host}/capacity"
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception:
            pass
        return {
            "used_bytes": None,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": None,
        }
