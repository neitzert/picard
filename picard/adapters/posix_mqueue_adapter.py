"""POSIX message queue adapter -- stores chunks in kernel message queues.

Each write_chunk creates a named queue /picard.<token> using mq_open()
with O_CREAT|O_RDWR. Queues appear as files in /dev/mqueue/ and persist
until mq_unlink or reboot.

Linux-only. No root required.
"""
from __future__ import annotations

import sys
if sys.platform != "linux":
    raise ImportError("posix_mqueue adapter requires Linux")

import asyncio
import ctypes
import ctypes.util
import json
import os
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "posix_mqueue"

# mq_open flags
_O_RDWR   = 0o2
_O_CREAT  = 0o100
_O_NONBLOCK = 0o4000

class _MqAttr(ctypes.Structure):
    _fields_ = [
        ("mq_flags",   ctypes.c_long),
        ("mq_maxmsg",  ctypes.c_long),
        ("mq_msgsize", ctypes.c_long),
        ("mq_curmsgs", ctypes.c_long),
        ("_pad",       ctypes.c_long * 4),
    ]

def _libc():
    lib = ctypes.CDLL("libc.so.6", use_errno=True)
    return lib


class PosixMqueueAdapter(Adapter):

    def __init__(self, *, max_msg_size: int = 8192) -> None:
        self._max = max_msg_size
        self._reg: dict[str, tuple[int, str]] = {}  # token -> (mqd, encoded_chunk)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            tags={"requires": "Linux", "substrate": "posix-message-queue"},
        )

    def _open(self, name: str, flags: int, mode: int = 0o600) -> int:
        libc = _libc()
        attr = _MqAttr(0, 1, self._max, 0)
        mqd = libc.mq_open(name.encode(), flags, ctypes.c_uint(mode),
                           ctypes.byref(attr))
        if mqd < 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))
        return mqd

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            libc = _libc()
            token = uuid.uuid4().hex[:16]
            name = f"/picard.{token}"
            mqd = self._open(name, _O_RDWR | _O_CREAT)
            data = encoded_chunk.encode("utf-8")
            if libc.mq_send(mqd, data, len(data), 0) != 0:
                err = ctypes.get_errno()
                libc.mq_close(mqd)
                libc.mq_unlink(name.encode())
                raise OSError(err, f"mq_send: {os.strerror(err)}")
            libc.mq_close(mqd)
            return token, name

        token, name = await asyncio.to_thread(_write)
        self._reg[token] = (0, encoded_chunk)  # mqd not held; queue persists
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token, "name": name}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token][1]

        def _read():
            libc = _libc()
            name = loc["name"]
            mqd = self._open(name, _O_RDWR)
            buf = ctypes.create_string_buffer(self._max)
            n = libc.mq_receive(mqd, buf, self._max, None)
            if n < 0:
                err = ctypes.get_errno()
                libc.mq_close(mqd)
                raise OSError(err, f"mq_receive: {os.strerror(err)}")
            data = buf.raw[:n]
            # Re-send so data remains in queue
            libc.mq_send(mqd, data, n, 0)
            libc.mq_close(mqd)
            return data.decode("utf-8")

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        token = loc["token"]
        self._reg.pop(token, None)
        def _del():
            libc = _libc()
            libc.mq_unlink(loc["name"].encode())
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
            if not os.path.ismount("/dev/mqueue"):
                return False
            libc = _libc()
            token = uuid.uuid4().hex[:8]
            name = f"/picard.hc.{token}"
            attr = _MqAttr(0, 1, 64, 0)
            mqd = libc.mq_open(name.encode(), _O_RDWR | _O_CREAT,
                                ctypes.c_uint(0o600), ctypes.byref(attr))
            if mqd < 0:
                return False
            libc.mq_close(mqd)
            libc.mq_unlink(name.encode())
            return True
        except Exception:
            return False

    async def close(self) -> None:
        pass
