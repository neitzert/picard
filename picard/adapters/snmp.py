"""SNMP adapter — stores chunks as OID values on a cooperating agent.

Each chunk is stored under a custom OID tree using SNMPv2c SET/GET.
The OID encodes the manifest identity, block, and chunk indices.

Operations are abstracted behind _snmp_set/_snmp_get/_snmp_walk so
the transport layer (raw UDP with BER encoding) can be tested or
replaced independently.
"""

from __future__ import annotations

import asyncio
import os
import struct
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_SYSDESCR_OID = "1.3.6.1.2.1.1.1.0"


def _manifest_oid_prefix(base_oid: str, manifest_id: str) -> str:
    parts = []
    for ch in manifest_id[:8]:
        parts.append(str(ord(ch)))
    return f"{base_oid}.{'.'.join(parts)}"


def _chunk_oid(base_oid: str, manifest_id: str, block: int, chunk: int) -> str:
    prefix = _manifest_oid_prefix(base_oid, manifest_id)
    return f"{prefix}.{block}.{chunk}"


class SnmpAdapter(Adapter):
    """Scatter chunks into SNMP OID values on a cooperating agent."""

    def __init__(
        self,
        host: str,
        port: int = 161,
        *,
        community: str = "private",
        base_oid: str = "1.3.6.1.4.1.99999.1",
        max_chunk_bytes: int = 4000,
        timeout: float = 5.0,
    ) -> None:
        self._host = host
        self._port = port
        self._community = community
        self._base_oid = base_oid
        self._max = max_chunk_bytes
        self._timeout = timeout

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="snmp",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=60,
        )

    async def _send_recv(self, packet: bytes) -> bytes:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bytes] = loop.create_future()

        class _Proto(asyncio.DatagramProtocol):
            def connection_made(self, transport):
                transport.sendto(packet)

            def datagram_received(self, data, addr):
                if not fut.done():
                    fut.set_result(data)

            def error_received(self, exc):
                if not fut.done():
                    fut.set_exception(exc)

        transport, _ = await loop.create_datagram_endpoint(
            _Proto, remote_addr=(self._host, self._port)
        )
        try:
            return await asyncio.wait_for(fut, timeout=self._timeout)
        finally:
            transport.close()

    @staticmethod
    def _encode_oid(oid: str) -> bytes:
        parts = [int(p) for p in oid.split(".")]
        encoded = bytes([40 * parts[0] + parts[1]])
        for p in parts[2:]:
            if p < 128:
                encoded += bytes([p])
            else:
                groups = []
                while p > 0:
                    groups.append(p & 0x7F)
                    p >>= 7
                groups.reverse()
                for i, g in enumerate(groups):
                    if i < len(groups) - 1:
                        encoded += bytes([g | 0x80])
                    else:
                        encoded += bytes([g])
        return encoded

    @staticmethod
    def _encode_length(length: int) -> bytes:
        if length < 0x80:
            return bytes([length])
        elif length < 0x100:
            return bytes([0x81, length])
        else:
            return bytes([0x82, (length >> 8) & 0xFF, length & 0xFF])

    def _build_snmp_set(self, oid: str, value: str, request_id: int) -> bytes:
        oid_bytes = self._encode_oid(oid)
        oid_tlv = bytes([0x06]) + self._encode_length(len(oid_bytes)) + oid_bytes
        val_bytes = value.encode()
        val_tlv = bytes([0x04]) + self._encode_length(len(val_bytes)) + val_bytes
        varbind = bytes([0x30]) + self._encode_length(len(oid_tlv) + len(val_tlv)) + oid_tlv + val_tlv
        varbind_list = bytes([0x30]) + self._encode_length(len(varbind)) + varbind

        req_id = struct.pack("!i", request_id)
        req_id_tlv = bytes([0x02, 0x04]) + req_id
        error_status = bytes([0x02, 0x01, 0x00])
        error_index = bytes([0x02, 0x01, 0x00])
        pdu_body = req_id_tlv + error_status + error_index + varbind_list
        pdu = bytes([0xA3]) + self._encode_length(len(pdu_body)) + pdu_body

        community = self._community.encode()
        community_tlv = bytes([0x04]) + self._encode_length(len(community)) + community
        version_tlv = bytes([0x02, 0x01, 0x01])
        msg_body = version_tlv + community_tlv + pdu
        return bytes([0x30]) + self._encode_length(len(msg_body)) + msg_body

    def _build_snmp_get(self, oid: str, request_id: int) -> bytes:
        oid_bytes = self._encode_oid(oid)
        oid_tlv = bytes([0x06]) + self._encode_length(len(oid_bytes)) + oid_bytes
        null_tlv = bytes([0x05, 0x00])
        varbind = bytes([0x30]) + self._encode_length(len(oid_tlv) + len(null_tlv)) + oid_tlv + null_tlv
        varbind_list = bytes([0x30]) + self._encode_length(len(varbind)) + varbind

        req_id = struct.pack("!i", request_id)
        req_id_tlv = bytes([0x02, 0x04]) + req_id
        error_status = bytes([0x02, 0x01, 0x00])
        error_index = bytes([0x02, 0x01, 0x00])
        pdu_body = req_id_tlv + error_status + error_index + varbind_list
        pdu = bytes([0xA0]) + self._encode_length(len(pdu_body)) + pdu_body

        community = self._community.encode()
        community_tlv = bytes([0x04]) + self._encode_length(len(community)) + community
        version_tlv = bytes([0x02, 0x01, 0x01])
        msg_body = version_tlv + community_tlv + pdu
        return bytes([0x30]) + self._encode_length(len(msg_body)) + msg_body

    def _build_snmp_getnext(self, oid: str, request_id: int) -> bytes:
        oid_bytes = self._encode_oid(oid)
        oid_tlv = bytes([0x06]) + self._encode_length(len(oid_bytes)) + oid_bytes
        null_tlv = bytes([0x05, 0x00])
        varbind = bytes([0x30]) + self._encode_length(len(oid_tlv) + len(null_tlv)) + oid_tlv + null_tlv
        varbind_list = bytes([0x30]) + self._encode_length(len(varbind)) + varbind

        req_id = struct.pack("!i", request_id)
        req_id_tlv = bytes([0x02, 0x04]) + req_id
        error_status = bytes([0x02, 0x01, 0x00])
        error_index = bytes([0x02, 0x01, 0x00])
        pdu_body = req_id_tlv + error_status + error_index + varbind_list
        pdu = bytes([0xA1]) + self._encode_length(len(pdu_body)) + pdu_body

        community = self._community.encode()
        community_tlv = bytes([0x04]) + self._encode_length(len(community)) + community
        version_tlv = bytes([0x02, 0x01, 0x01])
        msg_body = version_tlv + community_tlv + pdu
        return bytes([0x30]) + self._encode_length(len(msg_body)) + msg_body

    async def _snmp_set(self, oid: str, value: str) -> bool:
        req_id = int.from_bytes(os.urandom(4), "big") & 0x7FFFFFFF
        packet = self._build_snmp_set(oid, value, req_id)
        resp = await self._send_recv(packet)
        return len(resp) > 0

    async def _snmp_get(self, oid: str) -> str | None:
        req_id = int.from_bytes(os.urandom(4), "big") & 0x7FFFFFFF
        packet = self._build_snmp_get(oid, req_id)
        resp = await self._send_recv(packet)
        idx = resp.rfind(b"\x04")
        if idx < 0:
            return None
        length_byte = resp[idx + 1]
        start = idx + 2
        if length_byte & 0x80:
            num_bytes = length_byte & 0x7F
            length_val = int.from_bytes(resp[start : start + num_bytes], "big")
            start += num_bytes
        else:
            length_val = length_byte
        return resp[start : start + length_val].decode(errors="replace")

    async def _snmp_walk(self, prefix: str) -> list[tuple[str, str]]:
        results: list[tuple[str, str]] = []
        current_oid = prefix
        for _ in range(1000):
            req_id = int.from_bytes(os.urandom(4), "big") & 0x7FFFFFFF
            packet = self._build_snmp_getnext(current_oid, req_id)
            try:
                resp = await self._send_recv(packet)
            except Exception:
                break
            value = self._snmp_get_response_value(resp)
            next_oid = self._snmp_get_response_oid(resp)
            if next_oid is None or not next_oid.startswith(prefix + "."):
                break
            if value is not None:
                results.append((next_oid, value))
            current_oid = next_oid
        return results

    @staticmethod
    def _snmp_get_response_oid(resp: bytes) -> str | None:
        idx = resp.find(bytes([0x06]), 20)
        if idx < 0:
            return None
        length = resp[idx + 1]
        oid_bytes = resp[idx + 2 : idx + 2 + length]
        if len(oid_bytes) < 1:
            return None
        parts = [str(oid_bytes[0] // 40), str(oid_bytes[0] % 40)]
        i = 1
        while i < len(oid_bytes):
            val = 0
            while i < len(oid_bytes):
                val = (val << 7) | (oid_bytes[i] & 0x7F)
                if not (oid_bytes[i] & 0x80):
                    i += 1
                    break
                i += 1
            parts.append(str(val))
        return ".".join(parts)

    @staticmethod
    def _snmp_get_response_value(resp: bytes) -> str | None:
        idx = resp.rfind(b"\x04")
        if idx < 0:
            return None
        length_byte = resp[idx + 1]
        start = idx + 2
        if length_byte & 0x80:
            num_bytes = length_byte & 0x7F
            length_val = int.from_bytes(resp[start : start + num_bytes], "big")
            start += num_bytes
        else:
            length_val = length_byte
        return resp[start : start + length_val].decode(errors="replace")

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk = Chunk.decode(encoded_chunk)
        oid = _chunk_oid(
            self._base_oid,
            chunk.manifest_id,
            chunk.block_index,
            chunk.chunk_index,
        )
        await self._snmp_set(oid, encoded_chunk)
        return StoredChunkRef(adapter_name="snmp", location=oid)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        result = await self._snmp_get(ref.location)
        if result is None:
            raise ValueError(f"no value at OID {ref.location}")
        return result

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        try:
            return await self._snmp_set(ref.location, "")
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        prefix = _manifest_oid_prefix(self._base_oid, manifest_id)
        try:
            entries = await self._snmp_walk(prefix)
            return [
                StoredChunkRef(adapter_name="snmp", location=oid)
                for oid, val in entries
                if val.startswith(f"PCHK:")
            ]
        except Exception:
            return []

    async def health_check(self) -> bool:
        try:
            result = await self._snmp_get(_SYSDESCR_OID)
            return result is not None
        except Exception:
            return False
