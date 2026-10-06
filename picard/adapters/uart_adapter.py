"""UART/serial adapter — stores chunks over serial connections.

Sends framed data over UART with a length prefix and CRC-16 for
integrity.  Works with any serial port — USB-to-serial, RS-232,
embedded debug headers, even JTAG-to-UART bridges.

Requires: pip install pyserial
"""

from __future__ import annotations

import asyncio
import struct
import uuid
import zlib
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_FRAME_MAGIC = b"\xCA\xFE"
_HEADER_FMT = ">2sIH"  # magic(2) + length(4) + crc16(2)
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)


class UartAdapter(Adapter):
    """Scatter chunks over UART/serial links."""

    def __init__(
        self,
        *,
        port: str = "/dev/ttyUSB0",
        baudrate: int = 115200,
        timeout: float = 1.0,
        max_chunk_bytes: int = 4096,
    ) -> None:
        self._port = port
        self._baudrate = baudrate
        self._timeout = timeout
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}
        self._frame_counter = 0

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="uart",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=True,
            rate_limit_per_min=60,
        )

    @staticmethod
    def _crc16(data: bytes) -> int:
        return zlib.crc32(data) & 0xFFFF

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        from picard.chunker import Chunk

        try:
            chunk = Chunk.decode(encoded_chunk)
            key = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
        except ValueError:
            key = uuid.uuid4().hex

        data = encoded_chunk.encode()
        self._frame_counter += 1
        frame_id = self._frame_counter

        def _write():
            import serial

            crc = self._crc16(data)
            header = struct.pack(_HEADER_FMT, _FRAME_MAGIC, len(data), crc)
            ser = serial.Serial(self._port, self._baudrate, timeout=self._timeout)
            try:
                ser.write(header + data)
                ser.flush()
            finally:
                ser.close()

        await asyncio.to_thread(_write)
        loc = f"uart://{self._port}/{frame_id}"
        self._cache[loc] = encoded_chunk
        return StoredChunkRef(adapter_name="uart", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        cached = self._cache.get(ref.location)
        if cached is not None:
            return cached

        def _read():
            import serial

            ser = serial.Serial(self._port, self._baudrate, timeout=self._timeout)
            try:
                header = ser.read(_HEADER_SIZE)
                if len(header) < _HEADER_SIZE:
                    raise KeyError(f"no data: {ref.location}")
                magic, length, expected_crc = struct.unpack(_HEADER_FMT, header)
                if magic != _FRAME_MAGIC:
                    raise ValueError("bad frame magic")
                data = ser.read(length)
                if len(data) < length:
                    raise ValueError("truncated frame")
                actual_crc = UartAdapter._crc16(data)
                if actual_crc != expected_crc:
                    raise ValueError("CRC mismatch")
                return data.decode()
            finally:
                ser.close()

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="uart", location=loc)
            for loc in self._cache
            if manifest_id in loc
        ]

    async def health_check(self) -> bool:
        def _check():
            import serial

            try:
                ser = serial.Serial(self._port, self._baudrate, timeout=self._timeout)
                ser.close()
                return True
            except Exception:
                return False

        try:
            return await asyncio.to_thread(_check)
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
