"""SysV shared memory adapter -- stores chunks in kernel shared memory.

Each write_chunk calls shmget(IPC_PRIVATE, size, IPC_CREAT|0o666) to get
a shmid, shmat to map it, writes a 4-byte length prefix + data, shmdt.
The shmid is visible in 'ipcs -m' until shmctl(IPC_RMID).

Linux-only. No root required.
"""
from __future__ import annotations

import sys
if sys.platform != "linux":
    raise ImportError("sysv_shm adapter requires Linux")

import asyncio
import ctypes
import json
import os
import struct
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "sysv_shm"

_IPC_PRIVATE = 0
_IPC_CREAT   = 0o1000
_IPC_RMID    = 0
_SHMAT_FAILED = ctypes.c_void_p(-1).value  # (void *)-1 as Python int


def _libc():
    lib = ctypes.CDLL("libc.so.6", use_errno=True)
    lib.shmat.restype = ctypes.c_void_p
    lib.shmat.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    lib.shmdt.restype = ctypes.c_int
    lib.shmdt.argtypes = [ctypes.c_void_p]
    return lib


class SysvShmAdapter(Adapter):

    def __init__(self, *, max_chunk_bytes: int = 65536) -> None:
        self._max = max_chunk_bytes
        self._reg: dict[str, tuple[int, str]] = {}  # token -> (shmid, encoded_chunk)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            tags={"requires": "Linux", "substrate": "sysv-shared-memory"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            libc = _libc()
            token = uuid.uuid4().hex[:16]
            data = encoded_chunk.encode("utf-8")
            size = 4 + len(data)
            shmid = libc.shmget(_IPC_PRIVATE, size, _IPC_CREAT | 0o666)
            if shmid < 0:
                err = ctypes.get_errno()
                raise OSError(err, f"shmget: {os.strerror(err)}")
            ptr = libc.shmat(shmid, None, 0)
            if ptr is None or ptr == _SHMAT_FAILED:
                libc.shmctl(shmid, _IPC_RMID, None)
                raise OSError("shmat failed")
            buf = (ctypes.c_char * size).from_address(ptr)
            buf[:4] = struct.pack(">I", len(data))
            buf[4:size] = data
            libc.shmdt(ptr)
            return token, shmid

        token, shmid = await asyncio.to_thread(_write)
        self._reg[token] = (shmid, encoded_chunk)
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token, "shmid": shmid}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token][1]

        def _read():
            libc = _libc()
            shmid = loc["shmid"]
            ptr = libc.shmat(shmid, None, 0)
            if ptr is None or ptr == _SHMAT_FAILED:
                raise OSError("shmat failed (segment may have been removed)")
            hdr_buf = (ctypes.c_char * 4).from_address(ptr)
            length = struct.unpack(">I", bytes(hdr_buf))[0]
            data_buf = (ctypes.c_char * length).from_address(ptr + 4)
            data = bytes(data_buf).decode("utf-8")
            libc.shmdt(ptr)
            return data

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        token = loc["token"]
        self._reg.pop(token, None)
        def _del():
            libc = _libc()
            libc.shmctl(loc["shmid"], _IPC_RMID, None)
        await asyncio.to_thread(_del)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        from picard.chunker import Chunk
        refs = []
        for token, (shmid, data) in self._reg.items():
            try:
                if Chunk.decode(data).manifest_id == manifest_id:
                    refs.append(StoredChunkRef(adapter_name=_NAME,
                                               location=json.dumps({"token": token,
                                                "shmid": shmid})))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            libc = _libc()
            shmid = libc.shmget(_IPC_PRIVATE, 1, _IPC_CREAT | 0o666)
            if shmid < 0:
                return False
            libc.shmctl(shmid, _IPC_RMID, None)
            return True
        except Exception:
            return False

    async def close(self) -> None:
        for token in list(self._reg):
            entry = self._reg.pop(token, None)
            if entry is not None:
                shmid = entry[0]
                try:
                    _libc().shmctl(shmid, _IPC_RMID, None)
                except Exception:
                    pass
