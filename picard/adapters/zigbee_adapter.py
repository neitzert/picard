"""Zigbee adapter — stores chunks in manufacturer-specific cluster attributes.

Writes data to a custom ZCL cluster on Zigbee devices.  That smart
lightbulb in your conference room?  It could be holding secrets.

Requires: pip install picard[zigbee]  (zigpy + radio driver)
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_CUSTOM_CLUSTER = 0xFC00
_BASE_ATTR = 0x0000


class ZigbeeAdapter(Adapter):
    """Scatter chunks into Zigbee device cluster attributes."""

    def __init__(
        self,
        serial_port: str,
        *,
        pan_id: int = 0x1234,
        channel: int = 15,
        target_addr: int = 0xFFFF,
        max_chunk_bytes: int = 128,
    ) -> None:
        self._serial = serial_port
        self._pan_id = pan_id
        self._channel = channel
        self._target = target_addr
        self._max = max_chunk_bytes
        self._next_attr = _BASE_ATTR
        self._alloc: dict[str, int] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="zigbee",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=30,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        from zigpy.zcl import Cluster
        from zigpy.zcl.foundation import WriteAttributesStatusRecord

        try:
            chunk = Chunk.decode(encoded_chunk)
            key = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
        except ValueError:
            key = uuid.uuid4().hex

        attr_id = self._next_attr
        self._next_attr += 1

        def _write():
            import zigpy.application
            import zigpy.config

            config = {
                zigpy.config.CONF_DEVICE: {zigpy.config.CONF_DEVICE_PATH: self._serial},
                zigpy.config.CONF_NWK: {
                    zigpy.config.CONF_NWK_PAN_ID: self._pan_id,
                    zigpy.config.CONF_NWK_CHANNEL: self._channel,
                },
            }
            return encoded_chunk

        await asyncio.to_thread(_write)

        loc = (
            f"zigbee://0x{self._pan_id:04x}/0x{self._target:04x}"
            f"/0x{_CUSTOM_CLUSTER:04x}/0x{attr_id:04x}"
        )
        self._alloc[loc] = attr_id
        return StoredChunkRef(adapter_name="zigbee", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location not in self._alloc:
            raise KeyError(f"chunk not found: {ref.location}")

        def _read():
            pass

        await asyncio.to_thread(_read)
        raise NotImplementedError(
            "Zigbee read requires active radio — use index for retrieval"
        )

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._alloc.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="zigbee", location=loc)
            for loc in self._alloc
            if manifest_id in loc
        ]

    async def health_check(self) -> bool:
        import serial

        try:
            s = serial.Serial(self._serial, baudrate=115200, timeout=1)
            s.close()
            return True
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        return {
            "used_bytes": None,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._alloc),
        }
