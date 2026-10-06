"""Tor adapter — stores chunks via Tor hidden services (.onion).

Routes traffic through the Tor SOCKS proxy to reach onion endpoints:
    tor://{onion_address}/{path}/{chunk_id}

Requires: pip install picard[tor]  (aiohttp, aiohttp-socks)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class TorAdapter(Adapter):
    """Scatter chunks via Tor hidden services."""

    def __init__(
        self,
        onion_url: str,
        *,
        socks_proxy: str = "socks5h://127.0.0.1:9050",
        max_chunk_bytes: int = 65536,
    ) -> None:
        self._onion_url = onion_url.rstrip("/")
        self._socks_proxy = socks_proxy
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="tor",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    def _make_connector(self):
        from aiohttp_socks import ProxyConnector

        return ProxyConnector.from_url(self._socks_proxy)

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import aiohttp

        try:
            chunk = Chunk.decode(encoded_chunk)
            chunk_id = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
        except ValueError:
            chunk_id = uuid.uuid4().hex

        connector = self._make_connector()
        async with aiohttp.ClientSession(connector=connector) as session:
            url = f"{self._onion_url}/chunks/{chunk_id}"
            async with session.put(url, data=encoded_chunk.encode()) as resp:
                resp.raise_for_status()

        onion_host = self._onion_url.split("://", 1)[-1]
        location = f"tor://{onion_host}/chunks/{chunk_id}"
        return StoredChunkRef(adapter_name="tor", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import aiohttp

        path = ref.location.split("://", 1)[1]
        host_and_path = path.split("/", 1)
        url = f"{self._onion_url}/{host_and_path[1]}" if len(host_and_path) > 1 else self._onion_url

        connector = self._make_connector()
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                return await resp.text()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        import aiohttp

        path = ref.location.split("://", 1)[1]
        host_and_path = path.split("/", 1)
        url = f"{self._onion_url}/{host_and_path[1]}" if len(host_and_path) > 1 else self._onion_url

        try:
            connector = self._make_connector()
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.delete(url) as resp:
                    return resp.status in (200, 204)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        import aiohttp

        try:
            connector = self._make_connector()
            async with aiohttp.ClientSession(connector=connector) as session:
                url = f"{self._onion_url}/chunks/{manifest_id}/"
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json()
                    onion_host = self._onion_url.split("://", 1)[-1]
                    return [
                        StoredChunkRef(
                            adapter_name="tor",
                            location=f"tor://{onion_host}/chunks/{manifest_id}/{item}",
                        )
                        for item in data.get("chunks", [])
                    ]
        except Exception:
            return []

    async def health_check(self) -> bool:
        import asyncio
        import socket

        def _check():
            parts = self._socks_proxy.split("://", 1)[-1].rsplit(":", 1)
            host = parts[0]
            port = int(parts[1]) if len(parts) > 1 else 9050
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            try:
                sock.connect((host, port))
                return True
            except Exception:
                return False
            finally:
                sock.close()

        return await asyncio.to_thread(_check)
