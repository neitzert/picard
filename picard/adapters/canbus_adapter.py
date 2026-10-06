"""CAN bus adapter — stores chunks as CAN bus frames.

Hides data on the vehicle/industrial CAN bus.  Each chunk is split into
8-byte CAN frames and reassembled on read.  Works on cars, trucks,
factory equipment, medical devices — anywhere CAN runs.

Requires: pip install python-can
"""

from __future__ import annotations

import asyncio
import struct
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class CanBusAdapter(Adapter):
    """Scatter chunks as CAN bus data frames."""

    def __init__(
        self,
        *,
        interface: str = "can0",
        channel: str | None = None,
        bustype: str = "socketcan",
        bitrate: int = 500_000,
        arbitration_id: int = 0x7FF,
        max_chunk_bytes: int = 4096,
    ) -> None:
        self._interface = interface
        self._channel = channel or interface
        self._bustype = bustype
        self._bitrate = bitrate
        self._arb_id = arbitration_id
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="canbus",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=True,
            rate_limit_per_min=6000,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        from picard.chunker import Chunk

        try:
            chunk = Chunk.decode(encoded_chunk)
            key = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
        except ValueError:
            key = uuid.uuid4().hex

        data = encoded_chunk.encode()

        def _write():
            import can

            bus = can.Bus(channel=self._channel, interface=self._bustype, bitrate=self._bitrate)
            try:
                header = struct.pack(">I", len(data))
                payload = header + data
                seq = 0
                for i in range(0, len(payload), 8):
                    frame_data = payload[i : i + 8]
                    if len(frame_data) < 8:
                        frame_data = frame_data + b"\x00" * (8 - len(frame_data))
                    msg = can.Message(
                        arbitration_id=self._arb_id,
                        data=frame_data,
                        is_extended_id=False,
                    )
                    bus.send(msg)
                    seq += 1
            finally:
                bus.shutdown()
            return seq

        seq_count = await asyncio.to_thread(_write)
        loc = f"can://{self._interface}/{self._arb_id:#x}/{key}"
        self._cache[loc] = encoded_chunk
        return StoredChunkRef(adapter_name="canbus", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        cached = self._cache.get(ref.location)
        if cached is not None:
            return cached

        def _read():
            import can

            bus = can.Bus(channel=self._channel, interface=self._bustype, bitrate=self._bitrate)
            try:
                frames: list[bytes] = []
                total_len = None
                while True:
                    msg = bus.recv(timeout=2.0)
                    if msg is None:
                        break
                    if msg.arbitration_id != self._arb_id:
                        continue
                    frames.append(bytes(msg.data))
                    if total_len is None and len(b"".join(frames)) >= 4:
                        raw = b"".join(frames)
                        total_len = struct.unpack(">I", raw[:4])[0]
                    if total_len is not None:
                        raw = b"".join(frames)
                        if len(raw) >= 4 + total_len:
                            return raw[4 : 4 + total_len].decode()
                raise KeyError(f"chunk not found: {ref.location}")
            finally:
                bus.shutdown()

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="canbus", location=loc)
            for loc in self._cache
            if manifest_id in loc
        ]

    async def health_check(self) -> bool:
        def _check():
            import can

            try:
                bus = can.Bus(channel=self._channel, interface=self._bustype, bitrate=self._bitrate)
                bus.shutdown()
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
