"""SPI flash adapter — stores chunks in SPI flash memory.

Hides data in SPI flash chips (W25Q128, MX25L, etc.) found on nearly
every PCB.  Data survives power cycles and is invisible to the OS.

Requires: pip install spidev
"""

from __future__ import annotations

import asyncio
import struct
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_CMD_READ = 0x03
_CMD_PAGE_PROGRAM = 0x02
_CMD_WRITE_ENABLE = 0x06
_CMD_READ_STATUS = 0x05
_CMD_SECTOR_ERASE = 0x20
_CMD_JEDEC_ID = 0x9F


class SpiAdapter(Adapter):
    """Scatter chunks into SPI flash pages."""

    def __init__(
        self,
        *,
        bus: int = 0,
        device: int = 0,
        speed_hz: int = 1_000_000,
        flash_size: int = 1_048_576,
        page_size: int = 256,
        max_chunk_bytes: int = 4096,
    ) -> None:
        self._bus = bus
        self._device = device
        self._speed = speed_hz
        self._flash_size = flash_size
        self._page_size = page_size
        self._max = max_chunk_bytes
        self._offset_map: dict[str, int] = {}
        self._manifest_index: dict[str, list[str]] = {}
        self._next_offset = 0

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="spi",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=120,
        )

    def _open_spi(self):
        import spidev

        spi = spidev.SpiDev()
        spi.open(self._bus, self._device)
        spi.max_speed_hz = self._speed
        return spi

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        from picard.chunker import Chunk

        try:
            chunk = Chunk.decode(encoded_chunk)
            key = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
        except ValueError:
            key = uuid.uuid4().hex

        data = encoded_chunk.encode()
        offset = self._next_offset
        payload = struct.pack(">H", len(data)) + data
        self._next_offset += len(payload)

        def _write():
            spi = self._open_spi()
            try:
                for i in range(0, len(payload), self._page_size):
                    page = list(payload[i : i + self._page_size])
                    addr = offset + i
                    spi.xfer2([_CMD_WRITE_ENABLE])
                    spi.xfer2(
                        [_CMD_PAGE_PROGRAM, (addr >> 16) & 0xFF, (addr >> 8) & 0xFF, addr & 0xFF]
                        + page
                    )
                    while spi.xfer2([_CMD_READ_STATUS, 0])[1] & 0x01:
                        pass
            finally:
                spi.close()

        await asyncio.to_thread(_write)
        loc = f"spi://{self._bus}.{self._device}/{offset}"
        self._offset_map[loc] = offset
        self._manifest_index.setdefault(key.split("/")[0], []).append(loc)
        return StoredChunkRef(adapter_name="spi", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        offset = self._offset_map.get(ref.location)
        if offset is None:
            offset = int(ref.location.rsplit("/", 1)[-1])

        def _read():
            spi = self._open_spi()
            try:
                hdr = spi.xfer2(
                    [_CMD_READ, (offset >> 16) & 0xFF, (offset >> 8) & 0xFF, offset & 0xFF]
                    + [0] * 2
                )[4:]
                length = (hdr[0] << 8) | hdr[1]
                data_offset = offset + 2
                raw = spi.xfer2(
                    [_CMD_READ, (data_offset >> 16) & 0xFF, (data_offset >> 8) & 0xFF, data_offset & 0xFF]
                    + [0] * length
                )[4:]
                return bytes(raw[:length]).decode()
            finally:
                spi.close()

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._offset_map.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="spi", location=loc)
            for loc in self._manifest_index.get(manifest_id, [])
        ]

    async def health_check(self) -> bool:
        def _check():
            spi = self._open_spi()
            try:
                jedec = spi.xfer2([_CMD_JEDEC_ID, 0, 0, 0])
                return jedec[1] != 0x00 and jedec[1] != 0xFF
            finally:
                spi.close()

        try:
            return await asyncio.to_thread(_check)
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        return {
            "used_bytes": self._next_offset,
            "free_bytes": self._flash_size - self._next_offset,
            "total_bytes": self._flash_size,
            "chunk_count": len(self._offset_map),
        }
