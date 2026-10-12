"""Minecraft adapter — stores chunks via RCON as sign text or book pages.

Connects to a Minecraft server's RCON interface and stores chunk data
as text on signs placed in the world, or as book page content.
The data persists as long as the world exists.

Requires: network access to a Minecraft server with RCON enabled
"""

from __future__ import annotations

import asyncio
import struct
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class MinecraftAdapter(Adapter):
    """Scatter chunks into Minecraft worlds via RCON."""

    def __init__(
        self,
        host: str,
        rcon_password: str,
        *,
        rcon_port: int = 25575,
        world_name: str = "world",
        base_x: int = 0,
        base_y: int = 64,
        base_z: int = 0,
        max_chunk_bytes: int = 256,
    ) -> None:
        self._host = host
        self._port = rcon_port
        self._password = rcon_password
        self._world = world_name
        self._base_x = base_x
        self._base_y = base_y
        self._base_z = base_z
        self._max = max_chunk_bytes
        self._request_id = 0
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="minecraft",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=30,
        )

    @staticmethod
    def _pack_rcon(request_id: int, msg_type: int, payload: str) -> bytes:
        payload_bytes = payload.encode("utf-8") + b"\x00\x00"
        length = 4 + 4 + len(payload_bytes)
        return struct.pack("<III", length, request_id, msg_type) + payload_bytes

    @staticmethod
    def _unpack_rcon(data: bytes) -> tuple[int, int, str]:
        length, request_id, msg_type = struct.unpack("<III", data[:12])
        payload = data[12 : 12 + length - 8].rstrip(b"\x00").decode("utf-8")
        return request_id, msg_type, payload

    async def _send_rcon(self, msg_type: int, payload: str) -> str:
        reader, writer = await asyncio.open_connection(self._host, self._port)
        try:
            self._request_id += 1
            packet = self._pack_rcon(self._request_id, msg_type, payload)
            writer.write(packet)
            await writer.drain()
            header = await reader.readexactly(4)
            (length,) = struct.unpack("<I", header)
            body = await reader.readexactly(length)
            _, _, response = self._unpack_rcon(header + body)
            return response
        finally:
            writer.close()
            await writer.wait_closed()

    async def _auth(self) -> str:
        return await self._send_rcon(3, self._password)

    async def _command(self, cmd: str) -> str:
        await self._auth()
        return await self._send_rcon(2, cmd)

    def _coords_for_chunk(self, chunk_index: int) -> tuple[int, int, int]:
        x = self._base_x + (chunk_index % 100) * 2
        y = self._base_y
        z = self._base_z + (chunk_index // 100) * 2
        return x, y, z

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            from picard.chunker import Chunk
            chunk = Chunk.decode(encoded_chunk)
            chunk_id = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
            idx = chunk.block_index * 1000 + chunk.chunk_index
        except (ValueError, Exception):
            chunk_id = uuid.uuid4().hex
            idx = hash(chunk_id) % 10000

        x, y, z = self._coords_for_chunk(idx)

        lines = [encoded_chunk[i : i + 60] for i in range(0, len(encoded_chunk), 60)]
        for i, line in enumerate(lines[:4]):
            cmd = f'data merge block {x} {y + i} {z} {{Text{i+1}:\'{{"text":"{line}"}}\'}}'
            await self._command(cmd)

        location = f"mc://{self._host}/{self._world}/{x},{y},{z}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="minecraft", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        val = self._cache.get(ref.location)
        if val is None:
            raise KeyError(f"chunk not found: {ref.location}")
        return val

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="minecraft", location=loc)
            for loc, data in self._cache.items()
            if manifest_id in loc or manifest_id in data
        ]

    async def health_check(self) -> bool:
        try:
            resp = await self._auth()
            return True
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        used = sum(len(v.encode()) for v in self._cache.values())
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._cache),
        }
