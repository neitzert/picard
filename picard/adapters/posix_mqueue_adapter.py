"""POSIX message queue adapter -- stores chunks in kernel message queues.

Each chunk is written to a named POSIX message queue (``/dev/mqueue/picard.<token>``).
Queues persist in kernel memory until ``mq_unlink`` is called or the system reboots.
They are visible via ``ls /dev/mqueue/`` and ``ipcs -q``.

The write-then-close pattern keeps the queue alive without holding an open file
descriptor. Reads open the queue, receive the message (destructive), and immediately
re-send it so the queue remains populated for subsequent reads.

Requires: Linux with ``/dev/mqueue`` mounted (standard on all modern distributions).
No elevated privileges needed.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

if sys.platform != "linux":
    raise ImportError("posix_mqueue_adapter requires Linux")

_ADAPTER_NAME = "posix-mqueue"

# Load librt for POSIX mqueue syscalls
try:
    _librt = ctypes.CDLL("librt.so.1", use_errno=True)
except OSError:
    _librt = ctypes.CDLL("libc.so.6", use_errno=True)

# oflag constants
_O_RDONLY = 0
_O_RDWR = 0o2
_O_CREAT = 0o100
_O_NONBLOCK = 0o4000

# struct mq_attr { long mq_flags, mq_maxmsg, mq_msgsize, mq_curmsgs; }
class _MqAttr(ctypes.Structure):
    _fields_ = [
        ("mq_flags",   ctypes.c_long),
        ("mq_maxmsg",  ctypes.c_long),
        ("mq_msgsize", ctypes.c_long),
        ("mq_curmsgs", ctypes.c_long),
    ]

_librt.mq_open.restype = ctypes.c_int
_librt.mq_open.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_uint, ctypes.POINTER(_MqAttr)]
_librt.mq_close.restype = ctypes.c_int
_librt.mq_close.argtypes = [ctypes.c_int]
_librt.mq_unlink.restype = ctypes.c_int
_librt.mq_unlink.argtypes = [ctypes.c_char_p]
_librt.mq_send.restype = ctypes.c_int
_librt.mq_send.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint]
_librt.mq_receive.restype = ctypes.c_ssize_t
_librt.mq_receive.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint)]


def _mq_open_create(name: bytes, msgsize: int) -> int:
    attr = _MqAttr(mq_flags=0, mq_maxmsg=1, mq_msgsize=msgsize, mq_curmsgs=0)
    mqd = _librt.mq_open(name, _O_CREAT | _O_RDWR, 0o600, ctypes.byref(attr))
    if mqd == -1:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
    return mqd


def _mq_open_rdwr(name: bytes) -> int:
    mqd = _librt.mq_open(name, _O_RDWR, 0, None)
    if mqd == -1:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
    return mqd


def _check(ret: int, op: str) -> int:
    if ret == -1:
        raise OSError(ctypes.get_errno(), f"{op}: {os.strerror(ctypes.get_errno())}")
    return ret


class PosixMqueueAdapter(Adapter):
    """Scatter chunks into POSIX message queues in /dev/mqueue."""

    def __init__(self, *, max_chunk_bytes: int = 65536) -> None:
        self._max = max_chunk_bytes
        # qname -> (encoded_chunk, manifest_id)
        self._registry: dict[str, tuple[str, str]] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_ADAPTER_NAME,
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=500,
            tags={
                "substrate": "posix-mqueue",
                "requires": "Linux,/dev/mqueue",
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

        token = uuid.uuid4().hex[:16]
        qname = f"/picard.{token}".encode()

        mqd = _mq_open_create(qname, max(len(data), 1))
        try:
            _check(_librt.mq_send(mqd, data, len(data), 0), "mq_send")
        finally:
            _librt.mq_close(mqd)

        self._registry[qname.decode()] = (encoded_chunk, manifest_id)
        return StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"mqueue://{qname.decode()}")

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        qname = ref.location.removeprefix("mqueue://").encode()
        if qname.decode() not in self._registry:
            raise KeyError(f"posix-mqueue chunk not found: {ref.location}")

        entry = self._registry[qname.decode()]
        msgsize = len(entry[0].encode())
        buf = ctypes.create_string_buffer(msgsize + 1)

        mqd = _mq_open_rdwr(qname)
        try:
            n = _librt.mq_receive(mqd, buf, msgsize + 1, None)
            if n == -1:
                raise OSError(ctypes.get_errno(), f"mq_receive: {os.strerror(ctypes.get_errno())}")
            received = buf.raw[:n].decode()
            # Re-send to keep in queue for next read
            _librt.mq_send(mqd, buf.raw[:n], n, 0)
        finally:
            _librt.mq_close(mqd)

        return received

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        qname = ref.location.removeprefix("mqueue://")
        if qname not in self._registry:
            return False
        del self._registry[qname]
        ret = _librt.mq_unlink(qname.encode())
        return ret == 0

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        try:
            mqueue_dir = "/dev/mqueue"
            for fname in os.listdir(mqueue_dir):
                if not fname.startswith("picard."):
                    continue
                qname = f"/{fname}"
                entry = self._registry.get(qname)
                if entry and entry[1] == manifest_id:
                    refs.append(StoredChunkRef(
                        adapter_name=_ADAPTER_NAME,
                        location=f"mqueue://{qname}",
                    ))
        except Exception:
            pass
        return refs

    async def health_check(self) -> bool:
        return os.path.isdir("/dev/mqueue")

    async def close(self) -> None:
        for qname in list(self._registry):
            _librt.mq_unlink(qname.encode())
        self._registry.clear()
