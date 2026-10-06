"""I2C EEPROM adapter — stores chunks in I2C EEPROM devices.

Hides data in hardware memory chips on the I2C bus. Common EEPROMs
(24C256, AT24C512) are present on virtually every embedded board
and are rarely monitored for unexpected data.

Requires: pip install smbus2
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class I2cAdapter(Adapter):
    """Scatter chunks into I2C EEPROM pages."""

    def __init__(
        self,
        *,
        bus_number: int = 1,
        device_address: int = 0x50,
        page_size: int = 32,
        max_chunk_bytes: int = 256,
    ) -> None:
        self._bus = bus_number
        self._addr = device_address
        self._page_size = page_size
        self._max = max_chunk_bytes
        self._offset_map: dict[str, int] = {}
        self._manifest_index: dict[str, list[str]] = {}
        self._next_offset = 0

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="i2c",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=60,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        from picard.chunker import Chunk

        try:
            chunk = Chunk.decode(encoded_chunk)
            key = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
        except ValueError:
            key = uuid.uuid4().hex

        data = encoded_chunk.encode()
        offset = self._next_offset
        self._next_offset += len(data) + 2  # 2 bytes for length prefix

        def _write():
            import smbus2

            bus = smbus2.SMBus(self._bus)
            try:
                length = len(data)
                bus.write_i2c_block_data(
                    self._addr, offset, [length >> 8, length & 0xFF]
                )
                for i in range(0, length, self._page_size):
                    page = list(data[i : i + self._page_size])
                    bus.write_i2c_block_data(
                        self._addr, offset + 2 + i, page
                    )
            finally:
                bus.close()

        await asyncio.to_thread(_write)
        loc = f"i2c://{self._bus}/{self._addr:#x}/{offset}"
        self._offset_map[loc] = offset
        self._manifest_index.setdefault(key.split("/")[0], []).append(loc)
        return StoredChunkRef(adapter_name="i2c", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        offset = self._offset_map.get(ref.location)
        if offset is None:
            parts = ref.location.split("/")
            offset = int(parts[-1])

        def _read():
            import smbus2

            bus = smbus2.SMBus(self._bus)
            try:
                hdr = bus.read_i2c_block_data(self._addr, offset, 2)
                length = (hdr[0] << 8) | hdr[1]
                result = []
                for i in range(0, length, self._page_size):
                    block = bus.read_i2c_block_data(
                        self._addr,
                        offset + 2 + i,
                        min(self._page_size, length - i),
                    )
                    result.extend(block)
                return bytes(result[:length]).decode()
            finally:
                bus.close()

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._offset_map.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="i2c", location=loc)
            for loc in self._manifest_index.get(manifest_id, [])
        ]

    async def health_check(self) -> bool:
        def _check():
            import smbus2

            bus = smbus2.SMBus(self._bus)
            try:
                bus.read_byte(self._addr)
                return True
            except OSError:
                return False
            finally:
                bus.close()

        try:
            return await asyncio.to_thread(_check)
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        return {
            "used_bytes": self._next_offset,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._offset_map),
        }
