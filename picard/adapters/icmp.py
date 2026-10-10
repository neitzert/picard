"""ICMP adapter -- embeds chunks in ICMP echo payloads.

Chunks are sent as ICMP echo request payloads to a cooperating PICARD
cache daemon on the target host.  The
daemon stores inbound payloads and serves them back on request.

Data is ephemeral -- it lasts only as long as the remote cache is running.

Requires: raw socket privileges (root/Administrator).
No external dependencies (uses stdlib socket + struct).
"""

from __future__ import annotations

import asyncio
import os
import socket
import struct
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

# Wire protocol inside the ICMP echo payload:
#   MAGIC   4 bytes  b"PCHK"
#   CMD     1 byte   (see _CMD_*)
#   SEQ     2 bytes  big-endian request ID
#   KEY_LEN 2 bytes  big-endian
#   KEY     variable UTF-8 location key
#   DATA    rest     (for STORE / DATA responses)

_MAGIC = b"PCHK"
_HDR = struct.Struct("!4sBHH")  # magic, cmd, seq, key_len

_CMD_STORE = 0x01
_CMD_FETCH = 0x02
_CMD_ERASE = 0x03
_CMD_INDEX = 0x04
_CMD_OK    = 0x80
_CMD_DATA  = 0x81
_CMD_ERR   = 0x82
_CMD_LIST  = 0x83

_ICMP_ECHO_REQUEST = 8
_ICMP_ECHO_REPLY = 0
_ICMP_HDR = struct.Struct("!BBHHH")  # type, code, checksum, id, seq


def _icmp_checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    s = sum(struct.unpack("!%dH" % (len(data) // 2), data))
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return ~s & 0xFFFF


def _build_icmp_packet(payload: bytes, ident: int, seq: int) -> bytes:
    header = _ICMP_HDR.pack(_ICMP_ECHO_REQUEST, 0, 0, ident, seq)
    packet = header + payload
    chk = _icmp_checksum(packet)
    header = _ICMP_HDR.pack(_ICMP_ECHO_REQUEST, 0, chk, ident, seq)
    return header + payload


def _build_picard_payload(cmd: int, seq: int, key: str, data: bytes = b"") -> bytes:
    key_bytes = key.encode("utf-8")
    return _HDR.pack(_MAGIC, cmd, seq, len(key_bytes)) + key_bytes + data


def _parse_picard_payload(payload: bytes) -> tuple[int, int, str, bytes]:
    magic, cmd, seq, key_len = _HDR.unpack_from(payload)
    if magic != _MAGIC:
        raise ValueError("not a PICARD ICMP payload")
    offset = _HDR.size
    key = payload[offset : offset + key_len].decode("utf-8")
    data = payload[offset + key_len :]
    return cmd, seq, key, data


class IcmpAdapter(Adapter):
    """Embed chunks in ICMP echo payloads."""

    def __init__(
        self,
        target_host: str,
        *,
        timeout: float = 5.0,
        max_chunk_bytes: int = 1400,
    ) -> None:
        self._target = target_host
        self._timeout = timeout
        self._max = max_chunk_bytes
        self._ident = os.getpid() & 0xFFFF
        self._seq = 0

    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFFFF
        return self._seq

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="icmp",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            ttl_seconds=3600,
            rate_limit_per_min=30,
        )

    @staticmethod
    def _key(manifest_id: str, block_index: int, chunk_index: int) -> str:
        return f"{manifest_id}/{block_index}/{chunk_index}"

    def _send_recv(self, cmd: int, key: str, data: bytes = b"") -> tuple[int, str, bytes]:
        seq = self._next_seq()
        picard_payload = _build_picard_payload(cmd, seq, key, data)
        packet = _build_icmp_packet(picard_payload, self._ident, seq)

        sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
        try:
            sock.settimeout(self._timeout)
            sock.sendto(packet, (self._target, 0))

            while True:
                resp_data, addr = sock.recvfrom(65535)
                # Skip IP header (usually 20 bytes)
                ip_hdr_len = (resp_data[0] & 0x0F) * 4
                icmp_data = resp_data[ip_hdr_len:]
                icmp_type = icmp_data[0]
                if icmp_type != _ICMP_ECHO_REPLY:
                    continue
                resp_ident = struct.unpack("!H", icmp_data[4:6])[0]
                resp_seq = struct.unpack("!H", icmp_data[6:8])[0]
                if resp_ident != self._ident or resp_seq != seq:
                    continue
                picard_resp = icmp_data[_ICMP_HDR.size:]
                resp_cmd, _, resp_key, resp_body = _parse_picard_payload(picard_resp)
                return resp_cmd, resp_key, resp_body
        finally:
            sock.close()

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk = Chunk.decode(encoded_chunk)
        key = self._key(chunk.manifest_id, chunk.block_index, chunk.chunk_index)

        def _write():
            resp_cmd, _, _ = self._send_recv(_CMD_STORE, key, encoded_chunk.encode("utf-8"))
            if resp_cmd == _CMD_ERR:
                raise IOError("ICMP cache rejected STORE")

        await asyncio.to_thread(_write)
        loc = f"icmp://{self._target}/{key}"
        return StoredChunkRef(adapter_name="icmp", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        key = ref.location.split("://", 1)[1].split("/", 1)[1]

        def _read():
            resp_cmd, _, resp_data = self._send_recv(_CMD_FETCH, key)
            if resp_cmd == _CMD_ERR:
                raise KeyError(f"chunk not found: {ref.location}")
            return resp_data.decode("utf-8")

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        key = ref.location.split("://", 1)[1].split("/", 1)[1]

        def _delete():
            resp_cmd, _, _ = self._send_recv(_CMD_ERASE, key)
            return resp_cmd == _CMD_OK

        return await asyncio.to_thread(_delete)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        def _list():
            resp_cmd, _, resp_data = self._send_recv(_CMD_INDEX, manifest_id)
            if resp_cmd == _CMD_ERR or not resp_data:
                return []
            keys = resp_data.decode("utf-8").split("\n")
            return [
                StoredChunkRef(
                    adapter_name="icmp",
                    location=f"icmp://{self._target}/{k}",
                )
                for k in keys
                if k
            ]

        return await asyncio.to_thread(_list)

    async def health_check(self) -> bool:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
            sock.close()
            return True
        except (PermissionError, OSError):
            return False
