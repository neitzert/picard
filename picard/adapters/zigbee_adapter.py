"""Zigbee adapter — stores chunks in manufacturer-specific ZCL cluster attributes.

Writes data to a custom ZCL cluster (0xFC00) on Zigbee devices via a coordinator
radio. Each chunk occupies one attribute slot addressed by a sequential attr_id.
The reference encodes PAN ID, target address, cluster, and attribute ID.

This is a HAL-pattern adapter: the protocol logic and addressing are complete, but
the radio transport requires a platform driver. Subclass ZigbeeAdapter and implement
the three abstract methods below to connect a real coordinator (e.g. zigpy + EZSP,
zigpy-znp, or a custom AT-command driver).

Requires: pip install picard-store[zigbee]  (zigpy + radio driver)
"""

from __future__ import annotations

import asyncio
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_CUSTOM_CLUSTER = 0xFC00
_BASE_ATTR = 0x0000


class ZigbeeAdapter(Adapter):
    """Scatter chunks into Zigbee device ZCL cluster attributes.

    Subclass and implement _write_zcl_attribute, _read_zcl_attribute, and
    _delete_zcl_attribute to connect a real coordinator radio.
    """

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

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="zigbee",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=30,
        )

    # -- abstract transport hooks (subclass must implement) -------------------

    async def _write_zcl_attribute(self, attr_id: int, data: bytes) -> None:
        """Write data to attribute attr_id on cluster _CUSTOM_CLUSTER."""
        raise NotImplementedError(
            "ZigbeeAdapter requires a radio driver: subclass and implement "
            "_write_zcl_attribute (e.g. using zigpy + EZSP or zigpy-znp)"
        )

    async def _read_zcl_attribute(self, attr_id: int) -> bytes:
        """Read data from attribute attr_id on cluster _CUSTOM_CLUSTER."""
        raise NotImplementedError(
            "ZigbeeAdapter requires a radio driver: subclass and implement "
            "_read_zcl_attribute"
        )

    async def _delete_zcl_attribute(self, attr_id: int) -> bool:
        """Clear attribute attr_id (write empty bytes). Return True if succeeded."""
        raise NotImplementedError(
            "ZigbeeAdapter requires a radio driver: subclass and implement "
            "_delete_zcl_attribute"
        )

    # -- adapter interface ----------------------------------------------------

    def _loc(self, attr_id: int) -> str:
        return (
            f"zigbee://0x{self._pan_id:04x}/0x{self._target:04x}"
            f"/0x{_CUSTOM_CLUSTER:04x}/0x{attr_id:04x}"
        )

    def _attr_id_from_loc(self, location: str) -> int:
        return int(location.rsplit("/", 1)[-1], 16)

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        attr_id = self._next_attr
        self._next_attr += 1
        await self._write_zcl_attribute(attr_id, encoded_chunk.encode("utf-8"))
        return StoredChunkRef(adapter_name="zigbee", location=self._loc(attr_id))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        attr_id = self._attr_id_from_loc(ref.location)
        data = await self._read_zcl_attribute(attr_id)
        return data.decode("utf-8")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        try:
            attr_id = self._attr_id_from_loc(ref.location)
            return await self._delete_zcl_attribute(attr_id)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        raise NotImplementedError(
            "Zigbee ZCL has no enumerate API; list_chunks requires a subclass "
            "that maintains an out-of-band attribute index"
        )

    async def health_check(self) -> bool:
        try:
            import serial
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
            "chunk_count": None,
        }
