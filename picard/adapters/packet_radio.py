"""AX.25 packet radio adapter — stores chunks over RF data links.

Sends data via AX.25 UI frames through a TNC (Terminal Node Controller)
using the KISS protocol.  Works anywhere with line-of-sight RF —
no cellular, no internet, no infrastructure required.

Requires: a TNC (hardware or software like Direwolf) and a radio
"""

from __future__ import annotations

import asyncio
import struct
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_KISS_FEND = 0xC0
_KISS_FESC = 0xDB
_KISS_TFEND = 0xDC
_KISS_TFESC = 0xDD
_AX25_UI = 0x03
_AX25_PID_NONE = 0xF0


class PacketRadioAdapter(Adapter):
    """Scatter chunks as AX.25 UI frames via KISS TNC."""

    def __init__(
        self,
        *,
        callsign: str,
        dest_callsign: str = "PICARD",
        tnc_host: str = "localhost",
        tnc_port: int = 8001,
        max_chunk_bytes: int = 256,
    ) -> None:
        self._callsign = callsign.upper().ljust(6)[:6]
        self._dest = dest_callsign.upper().ljust(6)[:6]
        self._tnc_host = tnc_host
        self._tnc_port = tnc_port
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}
        self._seq = 0

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="packet-radio",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=10,
        )

    @staticmethod
    def _encode_callsign(call: str, ssid: int = 0, last: bool = False) -> bytes:
        encoded = bytes(c << 1 for c in call.encode())
        ssid_byte = 0x60 | ((ssid & 0x0F) << 1) | (0x01 if last else 0x00)
        return encoded + bytes([ssid_byte])

    @staticmethod
    def _kiss_escape(data: bytes) -> bytes:
        out = bytearray()
        for b in data:
            if b == _KISS_FEND:
                out.extend([_KISS_FESC, _KISS_TFEND])
            elif b == _KISS_FESC:
                out.extend([_KISS_FESC, _KISS_TFESC])
            else:
                out.append(b)
        return bytes(out)

    def _build_frame(self, info: bytes) -> bytes:
        dest_addr = self._encode_callsign(self._dest, ssid=0, last=False)
        src_addr = self._encode_callsign(self._callsign, ssid=0, last=True)
        frame = dest_addr + src_addr + bytes([_AX25_UI, _AX25_PID_NONE]) + info
        kiss_data = bytes([0x00]) + self._kiss_escape(frame)
        return bytes([_KISS_FEND]) + kiss_data + bytes([_KISS_FEND])

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        from picard.chunker import Chunk

        try:
            chunk = Chunk.decode(encoded_chunk)
            key = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
        except ValueError:
            key = uuid.uuid4().hex

        self._seq += 1
        info = encoded_chunk.encode()
        kiss_frame = self._build_frame(info)

        async def _write():
            reader, writer = await asyncio.open_connection(self._tnc_host, self._tnc_port)
            try:
                writer.write(kiss_frame)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        await _write()
        loc = f"ax25://{self._callsign.strip()}/{self._dest.strip()}/{self._seq}"
        self._cache[loc] = encoded_chunk
        return StoredChunkRef(adapter_name="packet-radio", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        cached = self._cache.get(ref.location)
        if cached is not None:
            return cached
        raise KeyError(f"chunk not in cache: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="packet-radio", location=loc)
            for loc in self._cache
            if manifest_id in loc
        ]

    async def health_check(self) -> bool:
        try:
            reader, writer = await asyncio.open_connection(self._tnc_host, self._tnc_port)
            writer.close()
            await writer.wait_closed()
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
