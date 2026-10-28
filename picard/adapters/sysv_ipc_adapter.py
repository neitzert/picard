"""SysV IPC shared memory adapter -- stores chunks in kernel shared memory segments.

Each chunk is written to a SysV shared memory segment allocated with ``shmget``.
Segments are identified by a kernel-assigned integer SHMID, visible via ``ipcs -m``.
Data persists in kernel memory until ``shmctl(IPC_RMID)`` is called or the system
reboots. No filesystem path, no file descriptor held after the initial write.

Each chunk stores a 4-byte big-endian length prefix followed by the raw chunk bytes.

Requires: Linux. No elevated privileges needed.
"""

from __future__ import annotations

import ctypes
import os
import sys
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

if sys.platform != "linux":
    raise ImportError("sysv_ipc_adapter requires Linux")

_ADAPTER_NAME = "sysv-ipc"

_libc = ctypes.CDLL("libc.so.6", use_errno=True)

IPC_PRIVATE = 0
IPC_CREAT   = 0o1000
IPC_RMID    = 0

_libc.shmget.restype = ctypes.c_int
_libc.shmget.argtypes = [ctypes.c_int, ctypes.c_size_t, ctypes.c_int]

_libc.shmat.restype = ctypes.c_void_p
_libc.shmat.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]

_libc.shmdt.restype = ctypes.c_int
_libc.shmdt.argtypes = [ctypes.c_void_p]

_libc.shmctl.restype = ctypes.c_int
_libc.shmctl.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]


def _check(ret: int, op: str) -> int:
    if ret == -1:
        raise OSError(ctypes.get_errno(), f"{op}: {os.strerror(ctypes.get_errno())}")
    return ret


class SysvIpcAdapter(Adapter):
    """Scatter chunks into SysV shared memory segments."""

    def __init__(self, *, max_chunk_bytes: int = 65536) -> None:
        self._max = max_chunk_bytes
        # shmid -> (encoded_chunk, manifest_id)
        self._registry: dict[int, tuple[str, str]] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_ADAPTER_NAME,
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=500,
            tags={
                "substrate": "sysv-shm",
                "requires": "Linux",
            },
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        data = encoded_chunk.encode()
        if len(data) > self._max:
            raise ValueError(f"Chunk too large: {len(data)} > {self._max}")
        try:
            manifest_id = Chunk.decode(encoded_chunk).manifest_id
        except Exception:
            manifest_id = ""

        total = 4 + len(data)
        shmid = _check(_libc.shmget(IPC_PRIVATE, total, IPC_CREAT | 0o666), "shmget")
        ptr = _libc.shmat(shmid, None, 0)
        if ptr == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_errno(), f"shmat: {os.strerror(ctypes.get_errno())}")

        payload = len(data).to_bytes(4, "big") + data
        ctypes.memmove(ptr, payload, len(payload))
        _libc.shmdt(ptr)

        self._registry[shmid] = (encoded_chunk, manifest_id)
        return StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"sysv-ipc://{shmid}")

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        shmid = int(ref.location.removeprefix("sysv-ipc://"))
        if shmid not in self._registry:
            raise KeyError(f"sysv-ipc chunk not found: {ref.location}")

        entry = self._registry[shmid]
        data_len = len(entry[0].encode())
        total = 4 + data_len

        ptr = _libc.shmat(shmid, None, 0)
        if ptr == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_errno(), f"shmat: {os.strerror(ctypes.get_errno())}")
        try:
            raw = ctypes.string_at(ptr, total)
            n = int.from_bytes(raw[:4], "big")
            return raw[4:4 + n].decode()
        finally:
            _libc.shmdt(ptr)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        shmid = int(ref.location.removeprefix("sysv-ipc://"))
        if shmid not in self._registry:
            return False
        del self._registry[shmid]
        ret = _libc.shmctl(shmid, IPC_RMID, None)
        return ret == 0

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"sysv-ipc://{shmid}")
            for shmid, (_, mid) in self._registry.items()
            if mid == manifest_id
        ]

    async def health_check(self) -> bool:
        try:
            shmid = _libc.shmget(IPC_PRIVATE, 1, IPC_CREAT | 0o666)
            if shmid == -1:
                return False
            _libc.shmctl(shmid, IPC_RMID, None)
            return True
        except Exception:
            return False

    async def close(self) -> None:
        for shmid in list(self._registry):
            _libc.shmctl(shmid, IPC_RMID, None)
        self._registry.clear()
