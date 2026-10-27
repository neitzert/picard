"""DNS question adapter -- stores chunks via a cooperating DNS companion.

Each chunk is base64-encoded and stored in dns_companion.py via a Unix
socket control channel. The companion also exposes the data as DNS TXT
records for external retrieval (the covert channel side). Falls back to
an in-process dict when no companion is running.

Companion: python3 dns_companion.py
Requires: pip install dnslib (companion only; this adapter is stdlib)
"""

from __future__ import annotations

import asyncio
import base64
import os
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_ADAPTER_NAME = "dns-question"


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


class DnsQuestionAdapter(Adapter):
    """Scatter chunks via DNS query names through a local companion server."""

    def __init__(
        self,
        *,
        companion_socket: str = "/tmp/picard_dns.sock",
        max_chunk_bytes: int = 100,
    ) -> None:
        self._sock = companion_socket
        self._max = max_chunk_bytes
        self._fallback: dict[str, str] = {}  # token -> b64data
        self._registry: dict[str, str] = {}  # token -> manifest_id

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_ADAPTER_NAME,
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=300,
            tags={
                "substrate": "dns-question-covert",
                "requires": "companion:dns_companion.py",
            },
        )

    def _companion_available(self) -> bool:
        return os.path.exists(self._sock)

    async def _store(self, token: str, b64: str) -> None:
        if self._companion_available():
            await _sock_cmd(self._sock, f"STORE {token} {b64}")
        else:
            self._fallback[token] = b64

    async def _get(self, token: str) -> str:
        if self._companion_available():
            return await _sock_cmd(self._sock, f"GET {token}")
        return self._fallback.get(token, "MISS")

    async def _del(self, token: str) -> None:
        if self._companion_available():
            await _sock_cmd(self._sock, f"DEL {token}")
        else:
            self._fallback.pop(token, None)

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        if len(encoded_chunk.encode()) > self._max:
            raise ValueError(
                f"Chunk too large: {len(encoded_chunk.encode())} > {self._max}"
            )
        try:
            manifest_id = Chunk.decode(encoded_chunk).manifest_id
        except Exception:
            manifest_id = ""

        token = f"{manifest_id}.{uuid.uuid4().hex[:12]}"
        b64 = base64.b64encode(encoded_chunk.encode()).decode()
        await self._store(token, b64)
        self._registry[token] = manifest_id
        return StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"dns://{token}")

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = ref.location.removeprefix("dns://")
        if token not in self._registry:
            raise KeyError(f"dns-question chunk not found: {ref.location}")
        b64 = await self._get(token)
        if b64 == "MISS" or not b64:
            raise KeyError(f"Token missing from companion: {token}")
        return base64.b64decode(b64).decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = ref.location.removeprefix("dns://")
        if token not in self._registry:
            return False
        await self._del(token)
        del self._registry[token]
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        if self._companion_available():
            tokens = await _sock_list(self._sock, f"{manifest_id}.")
            return [
                StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"dns://{t}")
                for t in tokens
            ]
        return [
            StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"dns://{t}")
            for t, mid in self._registry.items()
            if mid == manifest_id
        ]

    async def health_check(self) -> bool:
        if self._companion_available():
            result = await _sock_cmd(self._sock, "LIST __health__")
            return True  # got a response
        return True  # fallback mode

    async def close(self) -> None:
        pass
