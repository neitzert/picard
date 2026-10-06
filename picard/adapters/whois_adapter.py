"""WHOIS adapter — stores chunks in WHOIS object remarks fields.

Write via RIPE DB REST API or custom WHOIS server, read via WHOIS query:
    whois://{host}/{object_key}

Requires: standard library (asyncio)
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class WhoisAdapter(Adapter):
    """Scatter chunks into WHOIS records."""

    def __init__(
        self,
        host: str,
        *,
        port: int = 43,
        write_url: str | None = None,
        ripe_auth: str | None = None,
        max_chunk_bytes: int = 4096,
    ) -> None:
        self._host = host
        self._port = port
        self._write_url = write_url
        self._ripe_auth = ripe_auth
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="whois",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=10,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            obj_key = f"PCHK-{chunk.manifest_id[:8]}-{chunk.block_index}-{chunk.chunk_index}"
        except ValueError:
            obj_key = f"PCHK-{uuid.uuid4().hex[:16]}"

        if self._write_url:
            import aiohttp

            payload = {
                "objects": {
                    "object": [
                        {
                            "attributes": {
                                "attribute": [
                                    {"name": "descr", "value": obj_key},
                                    {"name": "remarks", "value": encoded_chunk},
                                ]
                            }
                        }
                    ]
                }
            }
            headers = {}
            if self._ripe_auth:
                headers["Authorization"] = f"Bearer {self._ripe_auth}"
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._write_url, json=payload, headers=headers
                ) as resp:
                    resp.raise_for_status()
        else:
            reader, writer = await asyncio.open_connection(self._host, self._port)
            try:
                writer.write(f"STORE {obj_key} {encoded_chunk}\r\n".encode())
                await writer.drain()
                await reader.readline()
            finally:
                writer.close()
                await writer.wait_closed()

        location = f"whois://{self._host}/{obj_key}"
        return StoredChunkRef(adapter_name="whois", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        obj_key = ref.location.rsplit("/", 1)[1]

        reader, writer = await asyncio.open_connection(self._host, self._port)
        try:
            writer.write(f"{obj_key}\r\n".encode())
            await writer.drain()
            data = await reader.read(self._max + 4096)
        finally:
            writer.close()
            await writer.wait_closed()

        text = data.decode()
        for line in text.splitlines():
            if line.startswith("remarks:"):
                return line.split(":", 1)[1].strip()
        raise KeyError(f"chunk not found in WHOIS response: {obj_key}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        obj_key = ref.location.rsplit("/", 1)[1]
        if self._write_url:
            import aiohttp

            headers = {}
            if self._ripe_auth:
                headers["Authorization"] = f"Bearer {self._ripe_auth}"
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.delete(
                        f"{self._write_url}/{obj_key}", headers=headers
                    ) as resp:
                        return resp.status in (200, 204)
            except Exception:
                return False
        return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        try:
            reader, writer = await asyncio.open_connection(self._host, self._port)
            writer.write(b"help\r\n")
            await writer.drain()
            data = await reader.read(1024)
            writer.close()
            await writer.wait_closed()
            return len(data) > 0
        except Exception:
            return False
