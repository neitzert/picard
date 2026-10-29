"""signalfd adapter -- stores chunks with Linux signalfd as kernel anchor.

Each write_chunk calls signalfd(-1, sigmask, SFD_CLOEXEC) via ctypes
with a mask over SIGRTMIN+N. The fd is held open and appears in
/proc/self/fdinfo/. Chunk data is held in-process registry.

Linux-only. No root required.
"""
from __future__ import annotations

import sys
if sys.platform != "linux":
    raise ImportError("signalfd adapter requires Linux")

import asyncio
import ctypes
import json
import os
import signal
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "signalfd"
_SFD_CLOEXEC = 0o2000000
_SIGRTMIN = signal.SIGRTMIN


def _libc():
    return ctypes.CDLL("libc.so.6", use_errno=True)


def _make_sigmask(signum: int) -> bytes:
    mask = bytearray(128)
    bit = signum - 1
    mask[bit // 8] |= 1 << (bit % 8)
    return bytes(mask)


class SignalfdAdapter(Adapter):

    def __init__(self) -> None:
        self._reg: dict[str, tuple[int, str]] = {}  # token -> (fd, encoded_chunk)
        self._next_rt = 0

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=False,
            ordered=False,
            tags={"requires": "Linux", "substrate": "signalfd-file-descriptor"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            libc = _libc()
            token = uuid.uuid4().hex[:16]
            # Use SIGRTMIN through SIGRTMAX (range ~32 signals on Linux)
            rt_max = signal.SIGRTMAX - signal.SIGRTMIN
            signum = _SIGRTMIN + (self._next_rt % max(rt_max, 1))
            self._next_rt += 1
            mask = _make_sigmask(signum)
            mask_p = ctypes.create_string_buffer(mask, len(mask))
            fd = libc.signalfd(-1, mask_p, _SFD_CLOEXEC)
            if fd < 0:
                err = ctypes.get_errno()
                raise OSError(err, f"signalfd: {os.strerror(err)}")
            return token, fd

        token, fd = await asyncio.to_thread(_write)
        self._reg[token] = (fd, encoded_chunk)
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = json.loads(ref.location)["token"]
        if token not in self._reg:
            raise KeyError(f"signalfd chunk not in registry: {token}")
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
            mask = _make_sigmask(_SIGRTMIN)
            mask_p = ctypes.create_string_buffer(mask, len(mask))
            fd = libc.signalfd(-1, mask_p, _SFD_CLOEXEC)
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
