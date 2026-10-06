"""Finger adapter — stores chunks via finger protocol (RFC 1288).

Write to a custom picard finger server, read via standard finger query:
    finger://{host}/{key}

Requires: standard library (asyncio)
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class FingerAdapter(Adapter):
    """Scatter chunks via finger protocol."""

    def __init__(
        self,
        host: str,
        *,
        port: int = 79,
        write_port: int = 7979,
        max_chunk_bytes: int = 4096,
    ) -> None:
        self._host = host
        self._port = port
        self._write_port = write_port
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="finger",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=60,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            key = f"{chunk.manifest_id}.{chunk.block_index}.{chunk.chunk_index}"
        except ValueError:
            key = uuid.uuid4().hex

        reader, writer = await asyncio.open_connection(self._host, self._write_port)
        try:
            writer.write(f"STORE {key}\r\n{encoded_chunk}\r\n".encode())
            await writer.drain()
            await reader.readline()
        finally:
            writer.close()
            await writer.wait_closed()

        location = f"finger://{self._host}/{key}"
        return StoredChunkRef(adapter_name="finger", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        key = ref.location.rsplit("/", 1)[1]
        reader, writer = await asyncio.open_connection(self._host, self._port)
        try:
            writer.write(f"{key}\r\n".encode())
            await writer.drain()
            data = await reader.read(self._max + 1024)
        finally:
            writer.close()
            await writer.wait_closed()
        return data.decode().strip()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        key = ref.location.rsplit("/", 1)[1]
        try:
            reader, writer = await asyncio.open_connection(
                self._host, self._write_port
            )
            try:
                writer.write(f"DELETE {key}\r\n".encode())
                await writer.drain()
                resp = await reader.readline()
                return b"OK" in resp
            finally:
                writer.close()
                await writer.wait_closed()
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        try:
            reader, writer = await asyncio.open_connection(
                self._host, self._write_port
            )
            try:
                writer.write(f"LIST {manifest_id}\r\n".encode())
                await writer.drain()
                data = await reader.read(65536)
                lines = data.decode().strip().splitlines()
                return [
                    StoredChunkRef(
                        adapter_name="finger",
                        location=f"finger://{self._host}/{line.strip()}",
                    )
                    for line in lines
                    if line.strip()
                ]
            finally:
                writer.close()
                await writer.wait_closed()
        except Exception:
            return []

    async def health_check(self) -> bool:
        try:
            reader, writer = await asyncio.open_connection(self._host, self._port)
            writer.write(b"\r\n")
            await writer.drain()
            await reader.readline()
            writer.close()
            await writer.wait_closed()
            return True
        except Exception:
            return False
