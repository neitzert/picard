"""APRS adapter — stores chunks as ham radio APRS messages.

Data travels over VHF/UHF ham radio via the APRS-IS internet gateway.
Ranges of 50+ miles are achievable with a $30 handheld radio and no
internet connection required for the RF leg.

Requires: amateur radio license (for transmission)
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class AprsAdapter(Adapter):
    """Scatter chunks as APRS messages via APRS-IS."""

    def __init__(
        self,
        *,
        callsign: str,
        passcode: str,
        server: str = "rotate.aprs2.net",
        port: int = 14580,
        ssid: str = "-9",
        dest_callsign: str = "APDPIC",
        max_chunk_bytes: int = 67,
    ) -> None:
        self._callsign = callsign
        self._passcode = passcode
        self._server = server
        self._port = port
        self._ssid = ssid
        self._dest = dest_callsign
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}
        self._msg_counter = 0

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="aprs",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=6,
        )

    def _login_line(self) -> str:
        return f"user {self._callsign}{self._ssid} pass {self._passcode} vers PICARD 1.0\r\n"

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        from picard.chunker import Chunk

        try:
            chunk = Chunk.decode(encoded_chunk)
            key = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
        except ValueError:
            key = uuid.uuid4().hex

        self._msg_counter += 1
        msg_num = self._msg_counter

        async def _write():
            reader, writer = await asyncio.open_connection(self._server, self._port)
            try:
                await reader.readline()
                writer.write(self._login_line().encode())
                await writer.drain()
                await reader.readline()
                src = f"{self._callsign}{self._ssid}"
                packet = f"{src}>APRS,TCPIP*::{self._dest:<9}:{encoded_chunk}{{{msg_num}\r\n"
                writer.write(packet.encode())
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        await _write()
        loc = f"aprs://{self._callsign}/{msg_num}"
        self._cache[loc] = encoded_chunk
        return StoredChunkRef(adapter_name="aprs", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        cached = self._cache.get(ref.location)
        if cached is not None:
            return cached
        raise KeyError(f"chunk not in cache: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="aprs", location=loc)
            for loc in self._cache
            if manifest_id in loc
        ]

    async def health_check(self) -> bool:
        try:
            reader, writer = await asyncio.open_connection(self._server, self._port)
            banner = await asyncio.wait_for(reader.readline(), timeout=5)
            writer.close()
            await writer.wait_closed()
            return b"#" in banner
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
