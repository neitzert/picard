"""Abstract Unix socket adapter -- stores chunks in the kernel socket namespace.

Each write_chunk binds a SOCK_DGRAM abstract socket to '\0picard.<token>'.
The socket is held open in a background daemon thread; the chunk lives in
the in-process registry and the socket name is the visible substrate.

Linux-only. No root required.
"""
from __future__ import annotations

import sys
if sys.platform != "linux":
    raise ImportError("abstract_socket adapter requires Linux")

import asyncio
import json
import os
import socket
import threading
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "abstract_socket"


class AbstractSocketAdapter(Adapter):

    def __init__(self) -> None:
        self._reg: dict[str, tuple[socket.socket, str]] = {}  # token -> (sock, encoded_chunk)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=False,
            ordered=False,
            tags={"requires": "Linux", "substrate": "abstract-unix-socket"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            token = uuid.uuid4().hex[:16]
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sock.bind(f"\0picard.{token}")
            return token, sock

        token, sock = await asyncio.to_thread(_write)
        self._reg[token] = (sock, encoded_chunk)
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = json.loads(ref.location)["token"]
        if token not in self._reg:
            raise KeyError(f"abstract socket chunk not in registry: {token}")
        return self._reg[token][1]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = json.loads(ref.location)["token"]
        entry = self._reg.pop(token, None)
        if entry:
            try:
                entry[0].close()
            except Exception:
                pass
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        from picard.chunker import Chunk
        refs = []
        for token, (_, data) in self._reg.items():
            try:
                if Chunk.decode(data).manifest_id == manifest_id:
                    refs.append(StoredChunkRef(adapter_name=_NAME,
                                               location=json.dumps({"token": token})))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            token = uuid.uuid4().hex[:8]
            s.bind(f"\0picard.hc.{token}")
            s.close()
            return True
        except Exception:
            return False

    async def close(self) -> None:
        for token in list(self._reg):
            entry = self._reg.pop(token, None)
            if entry:
                try:
                    entry[0].close()
                except Exception:
                    pass
