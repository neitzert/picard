"""USB HID adapter — stores chunks via HID reports to a cooperating device.

Each chunk is fragmented into HID output reports and sent to a device
that acts as a key-value store.  The device responds with HID input
reports containing ACK, DATA, or ERROR responses.

The internal _open_device / _send_report / _recv_report / _close_device
methods are abstract and intended to be mocked or overridden.
"""

from __future__ import annotations

import struct
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_OP_WRITE = 0x01
_OP_READ = 0x02
_OP_DELETE = 0x03
_OP_LIST = 0x04
_OP_ACK = 0x80
_OP_DATA = 0x81
_OP_ERROR = 0x82

_HEADER_FMT = "<BBH8s"
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)  # 12 bytes


class UsbHidAdapter(Adapter):
    """Scatter chunks into USB HID reports."""

    def __init__(
        self,
        *,
        vendor_id: int = 0xFFFF,
        product_id: int = 0x0001,
        report_size: int = 64,
        max_chunk_bytes: int = 8000,
    ) -> None:
        self._vendor_id = vendor_id
        self._product_id = product_id
        self._report_size = report_size
        self._max = max_chunk_bytes
        self._device: object | None = None

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="usb-hid",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=True,
            rate_limit_per_min=120,
        )

    async def _open_device(self) -> object:
        raise NotImplementedError("requires HID library")

    async def _send_report(self, report: bytes) -> None:
        raise NotImplementedError("requires HID library")

    async def _recv_report(self, timeout: float = 5.0) -> bytes:
        raise NotImplementedError("requires HID library")

    async def _close_device(self) -> None:
        raise NotImplementedError("requires HID library")

    async def _ensure_device(self):
        if self._device is None:
            self._device = await self._open_device()
        return self._device

    def _make_msg_id(self) -> bytes:
        return uuid.uuid4().bytes[:8]

    def _build_report(
        self, report_id: int, op: int, seq: int, msg_id: bytes, payload: bytes
    ) -> bytes:
        header = struct.pack(_HEADER_FMT, report_id, op, seq, msg_id)
        data = header + payload
        if len(data) < self._report_size:
            data += b"\x00" * (self._report_size - len(data))
        return data[: self._report_size]

    def _parse_report(self, data: bytes) -> tuple[int, int, int, bytes, bytes]:
        report_id, op, seq, msg_id = struct.unpack_from(_HEADER_FMT, data)
        payload = data[_HEADER_SIZE:]
        return report_id, op, seq, msg_id, payload

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        await self._ensure_device()
        msg_id = self._make_msg_id()
        chunk_bytes = encoded_chunk.encode("utf-8")
        payload_size = self._report_size - _HEADER_SIZE

        for seq in range(0, max(len(chunk_bytes), 1), payload_size):
            fragment = chunk_bytes[seq : seq + payload_size]
            report = self._build_report(0x01, _OP_WRITE, seq // payload_size, msg_id, fragment)
            await self._send_report(report)

        resp = await self._recv_report()
        _, op, _, _, _ = self._parse_report(resp)
        if op == _OP_ERROR:
            raise IOError("device returned error on write")

        return StoredChunkRef(adapter_name="usb-hid", location=msg_id.hex())

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        await self._ensure_device()
        msg_id = bytes.fromhex(ref.location)
        report = self._build_report(0x01, _OP_READ, 0, msg_id, b"")
        await self._send_report(report)

        fragments: dict[int, bytes] = {}
        while True:
            resp = await self._recv_report()
            _, op, seq, _, payload = self._parse_report(resp)
            if op == _OP_ERROR:
                raise ValueError(f"device error reading {ref.location}")
            if op == _OP_ACK:
                break
            if op == _OP_DATA:
                fragments[seq] = payload.rstrip(b"\x00")
                if len(fragments) > 1000:
                    break
            else:
                break

        data = b"".join(fragments[i] for i in sorted(fragments))
        return data.decode("utf-8")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        await self._ensure_device()
        msg_id = bytes.fromhex(ref.location)
        report = self._build_report(0x01, _OP_DELETE, 0, msg_id, b"")
        await self._send_report(report)

        resp = await self._recv_report()
        _, op, _, _, _ = self._parse_report(resp)
        return op == _OP_ACK

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        await self._ensure_device()
        msg_id = b"\x00" * 8
        payload = manifest_id.encode("utf-8")
        report = self._build_report(0x01, _OP_LIST, 0, msg_id, payload)
        await self._send_report(report)

        refs: list[StoredChunkRef] = []
        while True:
            resp = await self._recv_report()
            _, op, seq, _, payload = self._parse_report(resp)
            if op == _OP_ACK or op == _OP_ERROR:
                break
            if op == _OP_DATA:
                entry = payload.rstrip(b"\x00")
                if entry:
                    refs.append(
                        StoredChunkRef(
                            adapter_name="usb-hid",
                            location=entry.decode("utf-8"),
                        )
                    )
            else:
                break
        return refs

    async def health_check(self) -> bool:
        try:
            device = await self._open_device()
            await self._close_device()
            return True
        except Exception:
            return False
