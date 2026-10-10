"""OpenVPN adapter — store chunks through an OpenVPN tunnel.

Controls the tunnel via the OpenVPN management interface and sends
chunk data via HTTP to a storage endpoint on the VPN network.

Note: OpenVPN traffic on port 443 is indistinguishable from HTTPS at
the packet level, providing double-layer encryption for stored chunks.

Requires: pip install picard[openvpn]  (aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class OpenVpnAdapter(Adapter):
    """Scatter chunks through an OpenVPN tunnel."""

    def __init__(
        self,
        storage_host: str,
        *,
        management_host: str = "127.0.0.1",
        management_port: int = 7505,
        config_file: str | None = None,
        max_chunk_bytes: int = 65536,
    ) -> None:
        self._storage_host = storage_host.rstrip("/")
        self._mgmt_host = management_host
        self._mgmt_port = management_port
        self._config_file = config_file
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
            name="openvpn",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=500,
        )

    def _chunk_key(self, encoded_chunk: str) -> str:
        from picard.chunker import Chunk
        try:
            chunk = Chunk.decode(encoded_chunk)
            return f"{chunk.manifest_id}/{chunk.block_index}_{chunk.chunk_index}"
        except (ValueError, Exception):
            return uuid.uuid4().hex

    async def _mgmt_command(self, cmd: str) -> str:
        import asyncio

        reader, writer = await asyncio.open_connection(
            self._mgmt_host, self._mgmt_port
        )
        await reader.readline()  # banner
        writer.write(f"{cmd}\r\n".encode())
        await writer.drain()
        lines = []
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            text = line.decode().strip()
            if text == "END" or not line:
                break
            lines.append(text)
        writer.close()
        await writer.wait_closed()
        return "\n".join(lines)

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        key = self._chunk_key(encoded_chunk)
        url = f"{self._storage_host}/chunks/{key}"

        session = await self._get_session()
        async with session.put(url, data=encoded_chunk) as resp:
            resp.raise_for_status()

        loc = f"ovpn://{self._storage_host}/{key}"
        return StoredChunkRef(adapter_name="openvpn", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        parts = ref.location.replace("ovpn://", "").split("/", 1)
        key = parts[1] if len(parts) > 1 else ""
        url = f"{self._storage_host}/chunks/{key}"

        session = await self._get_session()
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.text()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        parts = ref.location.replace("ovpn://", "").split("/", 1)
        key = parts[1] if len(parts) > 1 else ""
        url = f"{self._storage_host}/chunks/{key}"

        try:
            session = await self._get_session()
            async with session.delete(url) as resp:
                return resp.status in (200, 204)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        try:
            session = await self._get_session()
            async with session.get(
                f"{self._storage_host}/chunks/",
                params={"prefix": manifest_id},
            ) as resp:
                resp.raise_for_status()
                result = await resp.json()

            return [
                StoredChunkRef(
                    adapter_name="openvpn",
                    location=f"ovpn://{self._storage_host}/{k}",
                )
                for k in result.get("keys", [])
            ]
        except Exception:
            return []

    async def health_check(self) -> bool:
        try:
            result = await self._mgmt_command("state")
            return "CONNECTED" in result
        except Exception:
            return False

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
