"""eventfd adapter -- stores chunks with eventfd kernel counters as anchors.

Each write_chunk creates an eventfd(1) with a unique initial value derived
from the chunk hash. The fd is held open; the eventfd value and fd number
appear in /proc/self/fdinfo/. Chunk data is held in-process registry.

Linux-only. No root required.
"""
from __future__ import annotations

import sys
if sys.platform != "linux":
    raise ImportError("eventfd adapter requires Linux")

import asyncio
import ctypes
import hashlib
import json
import os
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "eventfd"
_EFD_CLOEXEC = 0o2000000


def _libc():
    return ctypes.CDLL("libc.so.6", use_errno=True)


class EventfdAdapter(Adapter):

    def __init__(self) -> None:
        self._reg: dict[str, tuple[int, str]] = {}  # token -> (fd, encoded_chunk)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=False,
            ordered=False,
            tags={"requires": "Linux", "substrate": "eventfd-counter"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            libc = _libc()
            token = uuid.uuid4().hex[:16]
            # Initial value = low 32 bits of sha256(encoded_chunk), nonzero
            h = int.from_bytes(hashlib.sha256(encoded_chunk.encode()).digest()[:4], "big") or 1
            fd = libc.eventfd(h, _EFD_CLOEXEC)
            if fd < 0:
                err = ctypes.get_errno()
                raise OSError(err, os.strerror(err))
            return token, fd

        token, fd = await asyncio.to_thread(_write)
        self._reg[token] = (fd, encoded_chunk)
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = json.loads(ref.location)["token"]
        if token not in self._reg:
            raise KeyError(f"eventfd chunk not in registry: {token}")
        return self._reg[token][1]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = json.loads(ref.location)["token"]
        entry = self._reg.pop(token, None)
        if entry:
            try:
                os.close(entry[0])
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
            libc = _libc()
            fd = libc.eventfd(1, _EFD_CLOEXEC)
            if fd < 0:
                return False
            os.close(fd)
            return True
        except Exception:
            return False

    async def close(self) -> None:
        for token in list(self._reg):
            entry = self._reg.pop(token, None)
            if entry:
                try:
                    os.close(entry[0])
                except Exception:
                    pass
