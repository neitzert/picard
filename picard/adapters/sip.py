"""SIP adapter — stores chunks in SIP MESSAGE bodies (RFC 3428).

A cooperating SIP server receives chunks as MESSAGE request bodies
and stores them keyed by X-PCHK-Id.  Retrieval, deletion, and listing
use the same MESSAGE method with different X-PCHK-Op headers.

Requires: no extra dependencies (stdlib asyncio UDP).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class SipAdapter(Adapter):
    """Scatter chunks into SIP MESSAGE bodies over UDP."""

    def __init__(
        self,
        server_host: str,
        server_port: int = 5060,
        from_uri: str = "sip:picard@example.com",
        to_uri: str = "sip:store@example.com",
        *,
        max_chunk_bytes: int = 1200,
    ) -> None:
        self._server_host = server_host
        self._server_port = server_port
        self._from_uri = from_uri
        self._to_uri = to_uri
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="sip",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=30,
            ttl_seconds=3600,
        )

    def _build_message(
        self,
        method: str,
        extra_headers: dict[str, str] | None = None,
        body: str = "",
    ) -> str:
        branch = f"z9hG4bK{uuid.uuid4().hex[:12]}"
        tag = uuid.uuid4().hex[:8]
        call_id = f"{uuid.uuid4().hex[:16]}@picard"
        lines = [
            f"{method} {self._to_uri} SIP/2.0",
            f"Via: SIP/2.0/UDP 0.0.0.0:5060;branch={branch}",
            f"From: <{self._from_uri}>;tag={tag}",
            f"To: <{self._to_uri}>",
            f"Call-ID: {call_id}",
            f"CSeq: 1 {method}",
            "Content-Type: text/plain",
            f"Content-Length: {len(body.encode())}",
        ]
        if extra_headers:
            for k, v in extra_headers.items():
                lines.append(f"{k}: {v}")
        lines.append("")
        lines.append(body)
        return "\r\n".join(lines)

    @staticmethod
    def _parse_response(data: str) -> tuple[int, str]:
        parts = data.split("\r\n\r\n", 1)
        header_block = parts[0]
        body = parts[1] if len(parts) > 1 else ""
        status_line = header_block.split("\r\n", 1)[0]
        # e.g. "SIP/2.0 200 OK"
        tokens = status_line.split(" ", 2)
        code = int(tokens[1]) if len(tokens) >= 2 else 0
        return code, body

    async def _send_recv(self, message: str) -> str:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()

        class _Protocol(asyncio.DatagramProtocol):
            def datagram_received(self, data: bytes, addr: tuple) -> None:
                if not future.done():
                    future.set_result(data.decode())

            def error_received(self, exc: Exception) -> None:
                if not future.done():
                    future.set_exception(exc)

        transport, _ = await loop.create_datagram_endpoint(
            _Protocol,
            remote_addr=(self._server_host, self._server_port),
        )
        try:
            transport.sendto(message.encode())
            return await asyncio.wait_for(future, timeout=5.0)
        finally:
            transport.close()

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        msg_id = uuid.uuid4().hex[:16]
        msg = self._build_message(
            "MESSAGE",
            extra_headers={"X-PCHK-Op": "WRITE", "X-PCHK-Id": msg_id},
            body=encoded_chunk,
        )
        resp = await self._send_recv(msg)
        code, _ = self._parse_response(resp)
        if code != 200:
            raise RuntimeError(f"SIP write failed with status {code}")
        return StoredChunkRef(adapter_name="sip", location=msg_id)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        msg = self._build_message(
            "MESSAGE",
            extra_headers={"X-PCHK-Op": "READ", "X-PCHK-Id": ref.location},
        )
        resp = await self._send_recv(msg)
        code, body = self._parse_response(resp)
        if code != 200:
            raise RuntimeError(f"SIP read failed with status {code}")
        return body

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        msg = self._build_message(
            "MESSAGE",
            extra_headers={"X-PCHK-Op": "DELETE", "X-PCHK-Id": ref.location},
        )
        try:
            resp = await self._send_recv(msg)
            code, _ = self._parse_response(resp)
            return code == 200
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        msg = self._build_message(
            "MESSAGE",
            extra_headers={"X-PCHK-Op": "LIST"},
            body=manifest_id,
        )
        try:
            resp = await self._send_recv(msg)
            code, body = self._parse_response(resp)
            if code != 200 or not body.strip():
                return []
            return [
                StoredChunkRef(adapter_name="sip", location=line)
                for line in body.strip().split("\n")
                if line.strip()
            ]
        except Exception:
            return []

    async def health_check(self) -> bool:
        msg = self._build_message("OPTIONS")
        try:
            resp = await self._send_recv(msg)
            code, _ = self._parse_response(resp)
            return code > 0
        except Exception:
            return False
