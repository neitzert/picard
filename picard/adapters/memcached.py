"""Memcached adapter — stores chunks as key-value pairs via the text protocol.

Each chunk becomes a Memcached key whose value is the PCHK-encoded string.
Keys are prefixed and structured by manifest/block/chunk for easy identification.
Uses raw TCP with the Memcached text protocol — no external library needed.

Note: Memcached is ephemeral (LRU eviction, TTL expiry). Chunks may vanish.
"""

from __future__ import annotations

import asyncio
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class MemcachedAdapter(Adapter):
    """Scatter chunks into Memcached via the text protocol."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 11211,
        *,
        prefix: str = "picard:",
        ttl_seconds: int = 3600,
        max_chunk_bytes: int = 1_000_000,
    ) -> None:
        self._host = host
        self._port = port
        self._prefix = prefix
        self._ttl = ttl_seconds
        self._max = max_chunk_bytes
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def _ensure_connection(self):
        if self._writer is not None:
            return
        self._reader, self._writer = await asyncio.open_connection(
            self._host, self._port,
        )

    def _key(self, manifest_id: str, block: int, chunk_index: int) -> str:
        return f"{self._prefix}{manifest_id}:b{block}:c{chunk_index}"

    async def _send_cmd(self, cmd: str) -> str:
        await self._ensure_connection()
        self._writer.write(cmd.encode())
        await self._writer.drain()
        response = await self._reader.readline()
        return response.decode().strip()

    async def _send_store(self, cmd: str, data: bytes) -> str:
        await self._ensure_connection()
        self._writer.write(cmd.encode())
        self._writer.write(data)
        self._writer.write(b"\r\n")
        await self._writer.drain()
        response = await self._reader.readline()
        return response.decode().strip()

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="memcached",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=10000,
            ttl_seconds=self._ttl,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk = Chunk.decode(encoded_chunk)
        key = self._key(chunk.manifest_id, chunk.block_index, chunk.chunk_index)
        data = encoded_chunk.encode()
        cmd = f"set {key} 0 {self._ttl} {len(data)}\r\n"
        resp = await self._send_store(cmd, data)
        if resp != "STORED":
            raise RuntimeError(f"memcached SET failed: {resp}")
        return StoredChunkRef(adapter_name="memcached", location=key)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        await self._ensure_connection()
        cmd = f"get {ref.location}\r\n"
        self._writer.write(cmd.encode())
        await self._writer.drain()

        header = await self._reader.readline()
        header_str = header.decode().strip()
        if header_str == "END":
            raise ValueError(f"key not found: {ref.location}")

        parts = header_str.split()
        length = int(parts[3])
        data = await self._reader.readexactly(length)
        await self._reader.readline()  # trailing \r\n
        await self._reader.readline()  # END\r\n
        return data.decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        try:
            resp = await self._send_cmd(f"delete {ref.location}\r\n")
            return resp == "DELETED"
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        try:
            resp = await self._send_cmd("stats\r\n")
            return resp.startswith("STAT")
        except Exception:
            return False
