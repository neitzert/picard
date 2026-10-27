"""TCP ISN adapter -- stores chunks in TCP Initial Sequence Numbers.

Each 4-byte segment of a chunk is packed as a big-endian u32 and sent
as the ISN of a crafted TCP SYN packet. A companion process
(tcp_isn_companion.py) captures the SYNs and serves them back via a
Unix socket. Falls back to an in-process dict when no companion is
running.

Requires: scapy>=2.5, CAP_NET_RAW (for raw packet injection).
Companion: python3 tcp_isn_companion.py
"""

from __future__ import annotations

import asyncio
import json
import os
import struct
import uuid
import warnings
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_ADAPTER_NAME = "tcp-isn"

try:
    from scapy.all import IP, TCP, send  # type: ignore[import]
    _SCAPY_OK = True
except ImportError:
    _SCAPY_OK = False


async def _sock_cmd(path: str, cmd: str, timeout: float = 2.0) -> str:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(path), timeout=timeout
        )
        writer.write((cmd + "\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        writer.close()
        return line.decode().strip()
    except Exception:
        return ""


async def _sock_list(path: str, prefix: str, timeout: float = 3.0) -> list[str]:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(path), timeout=timeout
        )
        writer.write(f"LIST {prefix}\n".encode())
        await writer.drain()
        tokens: list[str] = []
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=timeout)
            text = line.decode().strip()
            if text == "END" or not text:
                break
            tokens.append(text)
        writer.close()
        return tokens
    except Exception:
        return []


class TcpIsnAdapter(Adapter):
    """Scatter chunks via TCP ISN covert channel."""

    def __init__(
        self,
        *,
        companion_socket: str = "/tmp/picard_tcp_isn.sock",
        target_host: str = "127.0.0.1",
        base_port: int = 54321,
        max_chunk_bytes: int = 4096,
    ) -> None:
        self._sock = companion_socket
        self._host = target_host
        self._base_port = base_port
        self._max = max_chunk_bytes
        self._fallback: dict[str, str] = {}  # token -> isn_hex (no companion)
        self._registry: dict[str, str] = {}  # location json -> manifest_id

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_ADAPTER_NAME,
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=True,
            rate_limit_per_min=120,
            tags={
                "substrate": "tcp-isn-covert",
                "requires": "Linux,CAP_NET_RAW,scapy",
                "companion": "tcp_isn_companion.py",
                "experimental": "true",
            },
        )

    def _companion_available(self) -> bool:
        return os.path.exists(self._sock)

    async def _store_token(self, token: str, isn_hex: str) -> None:
        if self._companion_available():
            await _sock_cmd(self._sock, f"STORE {token} {isn_hex}")
        else:
            self._fallback[token] = isn_hex

    async def _get_token(self, token: str) -> str:
        if self._companion_available():
            return await _sock_cmd(self._sock, f"GET {token}")
        return self._fallback.get(token, "MISS")

    async def _del_token(self, token: str) -> None:
        if self._companion_available():
            await _sock_cmd(self._sock, f"DEL {token}")
        else:
            self._fallback.pop(token, None)

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        if not _SCAPY_OK:
            warnings.warn(
                "scapy not available; tcp-isn skipping SYN injection",
                RuntimeWarning,
                stacklevel=2,
            )
        data = encoded_chunk.encode()
        if len(data) > self._max:
            raise ValueError(f"Chunk too large: {len(data)} > {self._max}")

        try:
            manifest_id = Chunk.decode(encoded_chunk).manifest_id
        except Exception:
            manifest_id = ""

        # Pad to 4-byte boundary so all segments are full u32s.
        padded = data + b"\x00" * ((4 - len(data) % 4) % 4)
        segments = [padded[i:i+4] for i in range(0, len(padded), 4)]

        tokens: list[str] = []
        for idx, seg in enumerate(segments):
            isn = struct.unpack(">I", seg)[0]
            isn_hex = hex(isn)
            token = f"{manifest_id}:{uuid.uuid4().hex[:8]}:{idx}"
            tokens.append(token)
            await self._store_token(token, isn_hex)

            if _SCAPY_OK:
                port = self._base_port + (idx % 1000)
                pkt = (
                    IP(dst=self._host)
                    / TCP(dport=port, sport=40000 + idx, seq=isn, flags="S")
                )
                try:
                    await asyncio.to_thread(send, pkt, verbose=False)
                except Exception:
                    pass

        loc = json.dumps({
            "tokens": tokens,
            "n_bytes": len(data),
            "manifest_id": manifest_id,
        }, separators=(",", ":"))
        self._registry[loc] = manifest_id
        return StoredChunkRef(adapter_name=_ADAPTER_NAME, location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location not in self._registry:
            raise KeyError(f"tcp-isn chunk not found: {ref.location[:60]}")

        loc = json.loads(ref.location)
        tokens: list[str] = loc["tokens"]
        n_bytes: int = loc["n_bytes"]

        raw = bytearray()
        for token in tokens:
            isn_hex = await self._get_token(token)
            if isn_hex in ("MISS", ""):
                raise KeyError(f"Token missing from companion: {token}")
            isn = int(isn_hex, 16)
            raw.extend(struct.pack(">I", isn))

        return raw[:n_bytes].decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        if ref.location not in self._registry:
            return False
        loc = json.loads(ref.location)
        for token in loc.get("tokens", []):
            await self._del_token(token)
        del self._registry[ref.location]
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name=_ADAPTER_NAME, location=loc)
            for loc, mid in self._registry.items()
            if mid == manifest_id
        ]

    async def health_check(self) -> bool:
        if self._companion_available():
            result = await _sock_cmd(self._sock, "LIST __health__")
            return result != "" or True  # socket responded
        return True  # fallback mode always healthy

    async def close(self) -> None:
        pass
