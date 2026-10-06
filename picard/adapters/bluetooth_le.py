"""Bluetooth LE advertisement adapter — broadcasts chunks as BLE adverts.

Each chunk is fragmented into BLE advertisement packets using custom
manufacturer-specific data.  A cooperating BLE receiver collects and
stores the fragments.

Requires a platform-specific BLE library (e.g. bleak) for real usage;
the internal methods are abstracted for testability.
"""

from __future__ import annotations

import struct
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_COMPANY_ID = 0xFFFF
_OP_WRITE = 0x01
_OP_READ = 0x02
_OP_DELETE = 0x03
_OP_LIST = 0x04
_HEADER_SIZE = 7  # company_id(2) + op(1) + msg_id(4)


class BluetoothLeAdapter(Adapter):
    """Scatter chunks into BLE advertisement packets."""

    def __init__(
        self,
        *,
        device_address: str | None = None,
        service_uuid: str = "0000FFFF-0000-1000-8000-00805F9B34FB",
        max_chunk_bytes: int = 20,
    ) -> None:
        self._device_address = device_address
        self._service_uuid = service_uuid
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="bluetooth-le",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=60,
            ttl_seconds=30,
        )

    async def _advertise(self, data: bytes, service_uuid: str) -> None:
        raise NotImplementedError("requires platform BLE library")

    async def _scan(self, service_uuid: str, timeout: float = 5.0) -> list[bytes]:
        raise NotImplementedError("requires platform BLE library")

    async def _stop_advertise(self) -> None:
        raise NotImplementedError("requires platform BLE library")

    def _make_msg_id(self) -> bytes:
        return uuid.uuid4().bytes[:4]

    def _build_packet(self, op: int, msg_id: bytes, seq: int, payload: bytes) -> bytes:
        return struct.pack("<HB4sH", _COMPANY_ID, op, msg_id, seq) + payload

    def _parse_packet(self, data: bytes) -> tuple[int, bytes, int, bytes]:
        company, op, msg_id, seq = struct.unpack_from("<HB4sH", data)
        payload = data[_HEADER_SIZE + 2:]
        return op, msg_id, seq, payload

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        msg_id = self._make_msg_id()
        chunk_bytes = encoded_chunk.encode("utf-8")
        payload_size = self._max - _HEADER_SIZE - 2
        if payload_size <= 0:
            payload_size = 1

        fragments = []
        for i in range(0, max(len(chunk_bytes), 1), payload_size):
            fragments.append(chunk_bytes[i : i + payload_size])

        for seq, frag in enumerate(fragments):
            packet = self._build_packet(_OP_WRITE, msg_id, seq, frag)
            await self._advertise(packet, self._service_uuid)

        return StoredChunkRef(
            adapter_name="bluetooth-le", location=msg_id.hex()
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        msg_id = bytes.fromhex(ref.location)
        read_req = self._build_packet(_OP_READ, msg_id, 0, b"")
        await self._advertise(read_req, self._service_uuid)

        raw_packets = await self._scan(self._service_uuid, timeout=5.0)
        fragments: dict[int, bytes] = {}
        for pkt in raw_packets:
            if len(pkt) < _HEADER_SIZE + 2:
                continue
            op, pkt_id, seq, payload = self._parse_packet(pkt)
            if pkt_id == msg_id:
                fragments[seq] = payload

        data = b"".join(fragments[i] for i in sorted(fragments))
        return data.decode("utf-8")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        try:
            await self._stop_advertise()
            return True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        try:
            raw_packets = await self._scan(self._service_uuid, timeout=5.0)
        except Exception:
            return []

        prefix = f"PCHK:1:{manifest_id}:"
        seen: set[str] = set()
        refs: list[StoredChunkRef] = []
        for pkt in raw_packets:
            if len(pkt) < _HEADER_SIZE + 2:
                continue
            op, msg_id, seq, payload = self._parse_packet(pkt)
            mid_hex = msg_id.hex()
            if mid_hex in seen:
                continue
            try:
                text = payload.decode("utf-8", errors="ignore")
                if text.startswith(prefix):
                    seen.add(mid_hex)
                    refs.append(
                        StoredChunkRef(adapter_name="bluetooth-le", location=mid_hex)
                    )
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            await self._scan(self._service_uuid, timeout=1.0)
            return True
        except Exception:
            return False
