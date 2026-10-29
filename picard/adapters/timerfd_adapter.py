"""timerfd adapter -- stores chunks with timerfd kernel timers as anchors.

Each write_chunk creates a timerfd_create(CLOCK_MONOTONIC) set to expire
in 30 days. The fd is held open and appears in /proc/self/fdinfo/. Chunk
data is held in-process registry.

Linux-only. No root required.
"""
from __future__ import annotations

import sys
if sys.platform != "linux":
    raise ImportError("timerfd adapter requires Linux")

import asyncio
import ctypes
import json
import os
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "timerfd"
_CLOCK_MONOTONIC = 1
_TFD_CLOEXEC     = 0o2000000


class _Timespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]


class _ItimerSpec(ctypes.Structure):
    _fields_ = [("it_interval", _Timespec), ("it_value", _Timespec)]


def _libc():
    return ctypes.CDLL("libc.so.6", use_errno=True)


class TimerfdAdapter(Adapter):

    def __init__(self) -> None:
        self._reg: dict[str, tuple[int, str]] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=False,
            ordered=False,
            tags={"requires": "Linux", "substrate": "timerfd-kernel-timer"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            libc = _libc()
            token = uuid.uuid4().hex[:16]
            fd = libc.timerfd_create(_CLOCK_MONOTONIC, _TFD_CLOEXEC)
            if fd < 0:
                err = ctypes.get_errno()
                raise OSError(err, os.strerror(err))
            # Set expiry to 30 days from now
            spec = _ItimerSpec()
            spec.it_value.tv_sec = 30 * 86400
            spec.it_value.tv_nsec = 0
            libc.timerfd_settime(fd, 0, ctypes.byref(spec), None)
            return token, fd

        token, fd = await asyncio.to_thread(_write)
        self._reg[token] = (fd, encoded_chunk)
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = json.loads(ref.location)["token"]
        if token not in self._reg:
            raise KeyError(f"timerfd chunk not in registry: {token}")
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
            fd = libc.timerfd_create(_CLOCK_MONOTONIC, _TFD_CLOEXEC)
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
