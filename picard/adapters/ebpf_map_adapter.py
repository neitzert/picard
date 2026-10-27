"""eBPF map adapter -- stores chunks in a pinned BPF_MAP_TYPE_HASH map.

Uses the Linux bpf(2) syscall directly via ctypes.  The map is pinned at
/sys/fs/bpf/picard/<map_name> and persists until explicitly removed or the
system reboots (bpffs is a pseudo-filesystem that survives process death
but not reboots unless re-mounted).

Requires: Linux, kernel >= 5.8, CAP_BPF (or root).
No external Python dependencies -- ctypes only.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import struct
import sys
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

if sys.platform != "linux":
    raise ImportError("ebpf_map_adapter requires Linux")

# BPF command constants
_BPF_MAP_CREATE      = 0
_BPF_MAP_LOOKUP_ELEM = 1
_BPF_MAP_UPDATE_ELEM = 2
_BPF_MAP_DELETE_ELEM = 3
_BPF_MAP_GET_NEXT_KEY = 4
_BPF_OBJ_PIN         = 6
_BPF_OBJ_GET         = 7

_BPF_MAP_TYPE_HASH = 1
_BPF_ANY           = 0  # create or update

# x86_64 syscall number for bpf(2)
_NR_BPF = 321

_KEY_SIZE   = 64       # token string, null-padded
_VALUE_SIZE = 65536    # chunk data, null-padded

_BPF_PIN_DIR = "/sys/fs/bpf/picard"

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


def _bpf(cmd: int, attr: ctypes.Structure, attr_size: int) -> int:
    ret = _libc.syscall(_NR_BPF, cmd, ctypes.byref(attr), attr_size)
    return int(ret)


class _BpfAttrCreate(ctypes.Structure):
    _fields_ = [
        ("map_type",    ctypes.c_uint32),
        ("key_size",    ctypes.c_uint32),
        ("value_size",  ctypes.c_uint32),
        ("max_entries", ctypes.c_uint32),
        ("map_flags",   ctypes.c_uint32),
    ]


class _BpfAttrElem(ctypes.Structure):
    _fields_ = [
        ("map_fd",  ctypes.c_uint32),
        ("key",     ctypes.c_uint64),
        ("value",   ctypes.c_uint64),
        ("flags",   ctypes.c_uint64),
    ]


class _BpfAttrObj(ctypes.Structure):
    _fields_ = [
        ("pathname", ctypes.c_uint64),
        ("bpf_fd",   ctypes.c_uint32),
        ("file_flags", ctypes.c_uint32),
    ]


def _create_map(max_entries: int = 65535) -> int:
    attr = _BpfAttrCreate(
        map_type=_BPF_MAP_TYPE_HASH,
        key_size=_KEY_SIZE,
        value_size=_VALUE_SIZE,
        max_entries=max_entries,
        map_flags=0,
    )
    fd = _bpf(_BPF_MAP_CREATE, attr, ctypes.sizeof(attr))
    if fd < 0:
        raise PermissionError(
            f"BPF_MAP_CREATE failed (errno {ctypes.get_errno()}). "
            "Requires CAP_BPF or root, kernel >= 5.8."
        )
    return fd


def _pin_map(fd: int, path: str) -> None:
    path_b = path.encode() + b"\x00"
    path_buf = ctypes.create_string_buffer(path_b)
    attr = _BpfAttrObj(
        pathname=ctypes.cast(path_buf, ctypes.c_void_p).value,
        bpf_fd=fd,
        file_flags=0,
    )
    ret = _bpf(_BPF_OBJ_PIN, attr, ctypes.sizeof(attr))
    if ret < 0:
        err = ctypes.get_errno()
        if err != 17:  # EEXIST is fine
            raise OSError(f"BPF_OBJ_PIN failed errno={err}")


def _get_pinned(path: str) -> int:
    path_b = path.encode() + b"\x00"
    path_buf = ctypes.create_string_buffer(path_b)
    attr = _BpfAttrObj(
        pathname=ctypes.cast(path_buf, ctypes.c_void_p).value,
        bpf_fd=0,
        file_flags=0,
    )
    fd = _bpf(_BPF_OBJ_GET, attr, ctypes.sizeof(attr))
    if fd < 0:
        raise FileNotFoundError(f"BPF_OBJ_GET failed for {path} errno={ctypes.get_errno()}")
    return fd


def _map_update(fd: int, key_bytes: bytes, value_bytes: bytes) -> None:
    k_buf = ctypes.create_string_buffer(key_bytes, _KEY_SIZE)
    v_buf = ctypes.create_string_buffer(value_bytes, _VALUE_SIZE)
    attr = _BpfAttrElem(
        map_fd=fd,
        key=ctypes.cast(k_buf, ctypes.c_void_p).value,
        value=ctypes.cast(v_buf, ctypes.c_void_p).value,
        flags=_BPF_ANY,
    )
    ret = _bpf(_BPF_MAP_UPDATE_ELEM, attr, ctypes.sizeof(attr))
    if ret < 0:
        raise OSError(f"BPF_MAP_UPDATE_ELEM failed errno={ctypes.get_errno()}")


def _map_lookup(fd: int, key_bytes: bytes) -> bytes:
    k_buf = ctypes.create_string_buffer(key_bytes, _KEY_SIZE)
    v_buf = ctypes.create_string_buffer(_VALUE_SIZE)
    attr = _BpfAttrElem(
        map_fd=fd,
        key=ctypes.cast(k_buf, ctypes.c_void_p).value,
        value=ctypes.cast(v_buf, ctypes.c_void_p).value,
        flags=0,
    )
    ret = _bpf(_BPF_MAP_LOOKUP_ELEM, attr, ctypes.sizeof(attr))
    if ret < 0:
        raise KeyError(f"BPF_MAP_LOOKUP_ELEM: key not found errno={ctypes.get_errno()}")
    return bytes(v_buf.raw).rstrip(b"\x00")


def _map_delete(fd: int, key_bytes: bytes) -> bool:
    k_buf = ctypes.create_string_buffer(key_bytes, _KEY_SIZE)
    attr = _BpfAttrElem(
        map_fd=fd,
        key=ctypes.cast(k_buf, ctypes.c_void_p).value,
        value=0,
        flags=0,
    )
    ret = _bpf(_BPF_MAP_DELETE_ELEM, attr, ctypes.sizeof(attr))
    return ret == 0


def _map_next_key(fd: int, prev_key_bytes: bytes | None) -> bytes | None:
    if prev_key_bytes is None:
        k_buf = None
        k_ptr = 0
    else:
        k_buf = ctypes.create_string_buffer(prev_key_bytes, _KEY_SIZE)
        k_ptr = ctypes.cast(k_buf, ctypes.c_void_p).value

    next_buf = ctypes.create_string_buffer(_KEY_SIZE)
    attr = _BpfAttrElem(
        map_fd=fd,
        key=k_ptr,
        value=ctypes.cast(next_buf, ctypes.c_void_p).value,
        flags=0,
    )
    ret = _bpf(_BPF_MAP_GET_NEXT_KEY, attr, ctypes.sizeof(attr))
    if ret < 0:
        return None  # no more keys
    return bytes(next_buf.raw)


class EbpfMapAdapter(Adapter):
    """Scatter chunks into a pinned BPF hash map."""

    def __init__(
        self,
        *,
        map_name: str = "picard_chunks",
        max_chunk_bytes: int = 65535,
    ) -> None:
        self._map_name = map_name
        self._max = max_chunk_bytes
        self._pin_path = os.path.join(_BPF_PIN_DIR, map_name)
        self._fd: int | None = None

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="ebpf_map",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=1000,
            tags={
                "requires": "Linux,CAP_BPF,kernel-5.8+",
                "substrate": "ebpf-map",
            },
        )

    def _open_fd(self) -> int:
        if self._fd is not None:
            return self._fd
        os.makedirs(_BPF_PIN_DIR, exist_ok=True)
        if os.path.exists(self._pin_path):
            self._fd = _get_pinned(self._pin_path)
        else:
            self._fd = _create_map()
            _pin_map(self._fd, self._pin_path)
        return self._fd

    def _key_bytes(self, token: str) -> bytes:
        raw = token.encode("utf-8")
        return raw[:_KEY_SIZE].ljust(_KEY_SIZE, b"\x00")

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        from picard.chunker import Chunk

        token = uuid.uuid4().hex
        fd = self._open_fd()
        value_bytes = encoded_chunk.encode("utf-8")
        _map_update(fd, self._key_bytes(token), value_bytes)
        location = f"ebpf://{self._map_name}/{token}"
        return StoredChunkRef(adapter_name="ebpf_map", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = ref.location.rsplit("/", 1)[-1]
        fd = self._open_fd()
        raw = _map_lookup(fd, self._key_bytes(token))
        return raw.decode("utf-8", errors="replace")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = ref.location.rsplit("/", 1)[-1]
        try:
            fd = self._open_fd()
        except Exception:
            return False
        return _map_delete(fd, self._key_bytes(token))

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        from picard.chunker import Chunk

        try:
            fd = self._open_fd()
        except Exception:
            return []

        refs: list[StoredChunkRef] = []
        prev: bytes | None = None
        while True:
            key = _map_next_key(fd, prev)
            if key is None:
                break
            prev = key
            token = key.rstrip(b"\x00").decode("utf-8", errors="replace")
            try:
                raw = _map_lookup(fd, key)
                data = raw.decode("utf-8", errors="replace")
                if Chunk.decode(data).manifest_id == manifest_id:
                    refs.append(
                        StoredChunkRef(
                            adapter_name="ebpf_map",
                            location=f"ebpf://{self._map_name}/{token}",
                        )
                    )
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        if not os.path.isdir("/sys/fs/bpf"):
            return False
        try:
            self._open_fd()
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except Exception:
                pass
            self._fd = None
