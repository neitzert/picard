"""CoAP adapter — stores chunks as resources on a CoAP server.

Each chunk becomes a resource at /picard/{manifest_id}/b{block}/c{chunk}
on a cooperating CoAP (RFC 7252) server over UDP.

No external dependencies — uses raw asyncio UDP sockets with minimal
CoAP message encoding.
"""

from __future__ import annotations

import asyncio
import os
import struct
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_CODE_GET = 0x01
_CODE_PUT = 0x03
_CODE_DELETE = 0x04

_OPT_URI_PATH = 11

_CLASS_SUCCESS = 2


class CoapAdapter(Adapter):
    """Scatter chunks into CoAP resources over UDP."""

    def __init__(
        self,
        host: str,
        port: int = 5683,
        *,
        max_chunk_bytes: int = 1000,
        timeout: float = 5.0,
    ) -> None:
        self._host = host
        self._port = port
        self._max = max_chunk_bytes
        self._timeout = timeout

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="coap",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=60,
        )

    @staticmethod
    def _resource_path(manifest_id: str, block: int, chunk: int) -> str:
        return f"/picard/{manifest_id}/b{block}/c{chunk}"

    @staticmethod
    def _encode_option(delta: int, value: bytes) -> bytes:
        length = len(value)
        d_nibble = delta if delta < 13 else (13 if delta < 269 else 14)
        l_nibble = length if length < 13 else (13 if length < 269 else 14)
        header = bytes([(d_nibble << 4) | l_nibble])
        ext = b""
        if delta >= 13 and delta < 269:
            ext += struct.pack("!B", delta - 13)
        elif delta >= 269:
            ext += struct.pack("!H", delta - 269)
        if length >= 13 and length < 269:
            ext += struct.pack("!B", length - 13)
        elif length >= 269:
            ext += struct.pack("!H", length - 269)
        return header + ext + value

    @classmethod
    def _build_coap_message(
        cls,
        code: int,
        uri_path: str,
        token: bytes,
        payload: bytes = b"",
    ) -> bytes:
        ver_type_tkl = (1 << 6) | (0 << 4) | len(token)
        msg_id = int.from_bytes(os.urandom(2), "big")
        header = struct.pack("!BBH", ver_type_tkl, code, msg_id) + token

        segments = [s for s in uri_path.split("/") if s]
        options = b""
        prev_num = 0
        for seg in segments:
            delta = _OPT_URI_PATH - prev_num
            options += cls._encode_option(delta, seg.encode())
            prev_num = _OPT_URI_PATH

        if payload:
            return header + options + b"\xff" + payload
        return header + options

    @staticmethod
    def _parse_coap_response(data: bytes) -> tuple[int, bytes]:
        if len(data) < 4:
            raise ValueError("CoAP response too short")
        tkl = data[0] & 0x0F
        code = data[1]
        offset = 4 + tkl
        while offset < len(data) and data[offset] != 0xFF:
            byte0 = data[offset]
            delta_nibble = (byte0 >> 4) & 0x0F
            length_nibble = byte0 & 0x0F
            offset += 1
            if delta_nibble == 13:
                offset += 1
            elif delta_nibble == 14:
                offset += 2
            if length_nibble == 13:
                opt_len = data[offset] + 13
                offset += 1
            elif length_nibble == 14:
                opt_len = struct.unpack("!H", data[offset : offset + 2])[0] + 269
                offset += 2
            else:
                opt_len = length_nibble
            offset += opt_len
        if offset < len(data) and data[offset] == 0xFF:
            return code, data[offset + 1 :]
        return code, b""

    async def _send_recv(self, msg: bytes) -> bytes:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bytes] = loop.create_future()

        class _Protocol(asyncio.DatagramProtocol):
            def datagram_received(self, data: bytes, addr: tuple) -> None:
                if not fut.done():
                    fut.set_result(data)

            def error_received(self, exc: Exception) -> None:
                if not fut.done():
                    fut.set_exception(exc)

        transport, _ = await loop.create_datagram_endpoint(
            _Protocol, remote_addr=(self._host, self._port)
        )
        try:
            transport.sendto(msg)
            return await asyncio.wait_for(fut, timeout=self._timeout)
        finally:
            transport.close()

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk = Chunk.decode(encoded_chunk)
        path = self._resource_path(
            chunk.manifest_id, chunk.block_index, chunk.chunk_index
        )
        token = os.urandom(4)
        msg = self._build_coap_message(
            _CODE_PUT, path, token, encoded_chunk.encode()
        )
        resp = await self._send_recv(msg)
        code, _ = self._parse_coap_response(resp)
        if (code >> 5) != _CLASS_SUCCESS:
            raise RuntimeError(f"CoAP PUT failed with code {code:#04x}")
        return StoredChunkRef(adapter_name="coap", location=path)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = os.urandom(4)
        msg = self._build_coap_message(_CODE_GET, ref.location, token)
        resp = await self._send_recv(msg)
        code, payload = self._parse_coap_response(resp)
        if (code >> 5) != _CLASS_SUCCESS:
            raise RuntimeError(f"CoAP GET failed with code {code:#04x}")
        return payload.decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = os.urandom(4)
        msg = self._build_coap_message(_CODE_DELETE, ref.location, token)
        try:
            resp = await self._send_recv(msg)
            code, _ = self._parse_coap_response(resp)
            return (code >> 5) == _CLASS_SUCCESS
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        path = f"/picard/{manifest_id}"
        token = os.urandom(4)
        msg = self._build_coap_message(_CODE_GET, path, token)
        try:
            resp = await self._send_recv(msg)
            code, payload = self._parse_coap_response(resp)
            if (code >> 5) != _CLASS_SUCCESS or not payload:
                return []
            lines = payload.decode().strip().splitlines()
            return [
                StoredChunkRef(adapter_name="coap", location=line.strip())
                for line in lines
                if line.strip()
            ]
        except Exception:
            return []

    async def health_check(self) -> bool:
        token = os.urandom(4)
        msg = self._build_coap_message(_CODE_GET, "/.well-known/core", token)
        try:
            resp = await self._send_recv(msg)
            code, _ = self._parse_coap_response(resp)
            return (code >> 5) == _CLASS_SUCCESS
        except Exception:
            return False
