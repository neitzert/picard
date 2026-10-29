"""POSIX semaphore adapter -- stores chunks with named semaphores as anchors.

Each write_chunk calls sem_open('/picard.<token>', O_CREAT) via ctypes and
sets the initial value to the low 32 bits of the chunk's SHA-256. The
semaphore is visible as /dev/shm/sem.picard.<token> on Linux. Chunk data is
held in-process registry.

Linux/macOS. No root required.
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import hashlib
import json
import os
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "posix_sem"

_O_CREAT = 0o100
_O_RDWR  = 0o2
_SEM_FAILED = ctypes.c_void_p(-1).value


def _libc():
    lib = ctypes.CDLL("libc.so.6", use_errno=True)
    lib.sem_open.restype = ctypes.c_void_p
    return lib


class PosixSemAdapter(Adapter):

    def __init__(self) -> None:
        self._reg: dict[str, tuple[ctypes.c_void_p, str]] = {}  # token -> (sem_ptr, chunk)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            tags={"requires": "Linux", "substrate": "posix-named-semaphore"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            libc = _libc()
            token = uuid.uuid4().hex[:16]
            name = f"/picard.{token}"
            # Initial value = low 31 bits of sha256 (sem values are nonneg int)
            h = int.from_bytes(hashlib.sha256(encoded_chunk.encode()).digest()[:4], "big") & 0x7FFFFFFF
            sem = libc.sem_open(name.encode(), _O_CREAT | _O_RDWR,
                                ctypes.c_uint(0o600), ctypes.c_uint(h))
            if sem is None or sem == _SEM_FAILED:
                err = ctypes.get_errno()
                raise OSError(err, f"sem_open: {os.strerror(err)}")
            return token, name, sem

        token, name, sem = await asyncio.to_thread(_write)
        self._reg[token] = (sem, encoded_chunk)
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token, "name": name}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = json.loads(ref.location)["token"]
        if token not in self._reg:
            raise KeyError(f"posix_sem chunk not in registry: {token}")
        return self._reg[token][1]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        token = loc["token"]
        entry = self._reg.pop(token, None)
        def _del():
            libc = _libc()
            if entry:
                libc.sem_close(entry[0])
            libc.sem_unlink(loc["name"].encode())
        await asyncio.to_thread(_del)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        from picard.chunker import Chunk
        refs = []
        for token, (_, data) in self._reg.items():
            try:
                if Chunk.decode(data).manifest_id == manifest_id:
                    refs.append(StoredChunkRef(adapter_name=_NAME,
                                               location=json.dumps({"token": token,
                                                "name": f"/picard.{token}"})))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            libc = _libc()
            token = uuid.uuid4().hex[:8]
            name = f"/picard.hc.{token}".encode()
            sem = libc.sem_open(name, _O_CREAT | _O_RDWR,
                                ctypes.c_uint(0o600), ctypes.c_uint(1))
            if sem is None or sem == _SEM_FAILED:
                return False
            libc.sem_close(sem)
            libc.sem_unlink(name)
            return True
        except Exception:
            return False

    async def close(self) -> None:
        for token in list(self._reg):
            entry = self._reg.pop(token, None)
            if entry:
                try:
                    _libc().sem_close(entry[0])
                except Exception:
                    pass
