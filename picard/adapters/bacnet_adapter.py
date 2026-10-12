"""BACnet adapter — stores chunks as property values on building automation devices.

Each chunk becomes a CharacterString value on a characterstringValue
object instance.

Requires: pip install picard-store[bacnet]  (BAC0 or bacpypes3)
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class BacnetAdapter(Adapter):
    """Scatter chunks into BACnet object properties."""

    def __init__(
        self,
        device_address: str,
        device_id: int,
        *,
        object_type: str = "characterstringValue",
        start_instance: int = 1000,
        max_chunk_bytes: int = 1024,
    ) -> None:
        self._address = device_address
        self._device_id = device_id
        self._obj_type = object_type
        self._start_instance = start_instance
        self._max = max_chunk_bytes
        self._next_instance = start_instance
        self._alloc: dict[str, int] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="bacnet",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=60,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import BAC0

        try:
            chunk = Chunk.decode(encoded_chunk)
            key = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
        except ValueError:
            key = uuid.uuid4().hex

        instance = self._next_instance
        self._next_instance += 1

        def _write():
            bacnet = BAC0.connect(ip=self._address)
            try:
                bacnet.write(
                    f"{self._address} {self._obj_type} {instance} presentValue {encoded_chunk}"
                )
            finally:
                bacnet.disconnect()

        await asyncio.to_thread(_write)

        loc = f"bacnet://{self._address}/{self._obj_type}/{instance}"
        self._alloc[loc] = instance
        return StoredChunkRef(adapter_name="bacnet", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import BAC0

        instance = self._alloc[ref.location]

        def _read():
            bacnet = BAC0.connect(ip=self._address)
            try:
                return bacnet.read(
                    f"{self._address} {self._obj_type} {instance} presentValue"
                )
            finally:
                bacnet.disconnect()

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        if ref.location not in self._alloc:
            return False
        import BAC0

        instance = self._alloc.pop(ref.location)

        def _delete():
            bacnet = BAC0.connect(ip=self._address)
            try:
                bacnet.write(
                    f"{self._address} {self._obj_type} {instance} presentValue "
                )
            finally:
                bacnet.disconnect()

        try:
            await asyncio.to_thread(_delete)
        except Exception:
            pass
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="bacnet", location=loc)
            for loc in self._alloc
            if manifest_id in loc
        ]

    async def health_check(self) -> bool:
        import BAC0

        def _check():
            bacnet = BAC0.connect(ip=self._address)
            try:
                bacnet.whois()
                return True
            finally:
                bacnet.disconnect()

        try:
            return await asyncio.to_thread(_check)
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        return {
            "used_bytes": None,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._alloc),
        }
