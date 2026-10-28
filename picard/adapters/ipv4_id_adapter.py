"""IPv4 Identification field covert channel adapter.

Encodes chunk data as the 16-bit IPv4 Identification field across a
sequence of UDP packets sent to loopback. Each packet carries 2 bytes.
A companion daemon (ipv4_id_companion.py) sniffs and records IP.id
sequences; without the companion the adapter falls back to an in-process
registry.

Requires: Linux (raw socket access). scapy optional.
Companion: ipv4_id_companion.py
"""

from __future__ import annotations

import asyncio
import base64
import sys
import uuid
from typing import Sequence

if sys.platform != "linux":
    raise ImportError("ipv4_id_adapter requires Linux")

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_ADAPTER_NAME = "ipv4-id"


async def _companion_cmd(sock_path: str, command: str) -> str | None:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(sock_path), timeout=1.0
        )
        writer.write((command + "\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return line.decode().strip()
    except Exception:
        return None


class Ipv4IdAdapter(Adapter):
    """Store chunks via IPv4 Identification field encoding (in-process fallback)."""

    def __init__(
        self,
        companion_socket: str = "/tmp/picard_ipv4_id.sock",
        target_host: str = "127.0.0.1",
        target_port: int = 19877,
        *,
        max_chunk_bytes: int = 65536,
    ) -> None:
        self._sock = companion_socket
        self._host = target_host
        self._port = target_port
        self._max = max_chunk_bytes
        self._registry: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_ADAPTER_NAME,
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            tags={
                "substrate": "ipv4-id-field",
                "requires": "Linux,scapy(optional),companion(optional)",
                "covert-channel": "true",
            },
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        if len(encoded_chunk.encode()) > self._max:
            raise ValueError(f"Chunk too large: {len(encoded_chunk.encode())} > {self._max}")
        try:
            manifest_id = Chunk.decode(encoded_chunk).manifest_id
        except Exception:
            manifest_id = ""

        token = uuid.uuid4().hex[:16]
        data = encoded_chunk.encode()

        b64 = base64.b64encode(data).decode()
        resp = await _companion_cmd(self._sock, f"STORE {token} {b64}")
        if resp != "OK":
            self._registry[token] = encoded_chunk
            # Best-effort scapy fire-and-forget
            words = [
                int.from_bytes(data[i : i + 2].ljust(2, b"\x00"), "big")
                for i in range(0, len(data), 2)
            ]
            try:
                from scapy.all import IP, UDP, Raw, send  # type: ignore[import]
                for word in words:
                    send(
                        IP(id=word, dst=self._host)
                        / UDP(dport=self._port)
                        / Raw(b"picard"),
                        verbose=False,
                    )
            except Exception:
                pass
        else:
            self._registry[token] = encoded_chunk

        return StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"ipv4-id://{token}")

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = ref.location.removeprefix("ipv4-id://")
        if token in self._registry:
            return self._registry[token]
        resp = await _companion_cmd(self._sock, f"GET {token}")
        if resp and resp.startswith("DATA "):
            return base64.b64decode(resp[5:]).decode()
        raise KeyError(f"ipv4-id chunk not found: {token}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = ref.location.removeprefix("ipv4-id://")
        existed = token in self._registry
        self._registry.pop(token, None)
        await _companion_cmd(self._sock, f"DEL {token}")
        return existed

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        for tok, chunk in self._registry.items():
            try:
                if Chunk.decode(chunk).manifest_id == manifest_id:
                    refs.append(StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"ipv4-id://{tok}"))
            except Exception:
                continue
        resp = await _companion_cmd(self._sock, f"LIST {manifest_id}")
        if resp and resp.startswith("TOKENS "):
            tokens_str = resp[7:].strip()
            if tokens_str:
                seen = {r.location.removeprefix("ipv4-id://") for r in refs}
                for tok in tokens_str.split(","):
                    if tok and tok not in seen:
                        refs.append(StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"ipv4-id://{tok}"))
        return refs

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        pass
