"""memfd adapter -- stores chunks in anonymous kernel memory files.

Each write_chunk calls memfd_create('picard.<token>', 0) to get an
anonymous file descriptor. The chunk is written to the fd; it is visible
as /proc/self/fd/<N> -> 'memfd:picard.<token> (deleted)' and lives until
the fd is closed.

Linux 3.17+. No root required.
"""
from __future__ import annotations

import sys
if sys.platform != "linux":
    raise ImportError("memfd adapter requires Linux")

import asyncio
import ctypes
import json
import os
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "memfd"
_MFD_CLOEXEC = 1


def _libc():
    return ctypes.CDLL("libc.so.6", use_errno=True)


def _memfd_create(name: str) -> int:
    libc = _libc()
    fd = libc.memfd_create(name.encode(), _MFD_CLOEXEC)
    if fd < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return fd


class MemfdAdapter(Adapter):

    def __init__(self) -> None:
        self._reg: dict[str, tuple[int, str]] = {}  # token -> (fd, encoded_chunk)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=False,
            ordered=False,
            tags={"requires": "Linux 3.17+", "substrate": "memfd-anonymous-file"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            token = uuid.uuid4().hex[:16]
            fd = _memfd_create(f"picard.{token}")
            data = encoded_chunk.encode("utf-8")
            os.write(fd, data)
            return token, fd

        token, fd = await asyncio.to_thread(_write)
        self._reg[token] = (fd, encoded_chunk)
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = json.loads(ref.location)["token"]
        if token not in self._reg:
            raise KeyError(f"memfd chunk not in registry: {token}")
        fd, _ = self._reg[token]

        def _read():
            os.lseek(fd, 0, os.SEEK_SET)
            return os.read(fd, 65536).decode("utf-8")

        return await asyncio.to_thread(_read)

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
            fd = _memfd_create("picard.hc")
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
