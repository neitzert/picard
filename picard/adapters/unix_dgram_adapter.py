"""UNIX datagram socket adapter -- stores chunks as pending datagrams.

Each write_chunk creates a UNIX SOCK_DGRAM socket pair. The encoded chunk
is sent to the peer; the datagram sits in the kernel receive buffer until
read_chunk consumes and re-sends it. Data lives in the kernel socket buffer.

Linux-only. No root required.
"""
from __future__ import annotations

import sys
if sys.platform != "linux":
    raise ImportError("unix_dgram adapter requires Linux")

import asyncio
import json
import os
import socket
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "unix_dgram"


class UnixDgramAdapter(Adapter):

    def __init__(self) -> None:
        self._reg: dict[str, tuple[socket.socket, socket.socket, str]] = {}
        # token -> (sender_sock, receiver_sock, encoded_chunk)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=False,
            ordered=False,
            tags={"requires": "Linux", "substrate": "unix-dgram-socket-buffer"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            token = uuid.uuid4().hex[:16]
            # Use socketpair for anonymous UNIX SOCK_DGRAM pair
            s1, s2 = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
            s1.sendall(encoded_chunk.encode("utf-8"))
            return token, s1, s2

        token, s1, s2 = await asyncio.to_thread(_write)
        self._reg[token] = (s1, s2, encoded_chunk)
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = json.loads(ref.location)["token"]
        if token not in self._reg:
            raise KeyError(f"unix_dgram chunk not in registry: {token}")
        s1, s2, _ = self._reg[token]

        def _read():
            # Receive from s2 and immediately re-send from s1 so data persists
            data = s2.recv(65536)
            s1.sendall(data)
            return data.decode("utf-8")

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = json.loads(ref.location)["token"]
        entry = self._reg.pop(token, None)
        if entry:
            for s in (entry[0], entry[1]):
                try:
                    s.close()
                except Exception:
                    pass
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        from picard.chunker import Chunk
        refs = []
        for token, (_, _, data) in self._reg.items():
            try:
                if Chunk.decode(data).manifest_id == manifest_id:
                    refs.append(StoredChunkRef(adapter_name=_NAME,
                                               location=json.dumps({"token": token})))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            s1, s2 = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
            s1.sendall(b"test")
            data = s2.recv(8)
            s1.close()
            s2.close()
            return data == b"test"
        except Exception:
            return False

    async def close(self) -> None:
        for token in list(self._reg):
            entry = self._reg.pop(token, None)
            if entry:
                for s in (entry[0], entry[1]):
                    try:
                        s.close()
                    except Exception:
                        pass
