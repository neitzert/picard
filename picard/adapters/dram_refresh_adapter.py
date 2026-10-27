"""DRAM refresh adapter -- stores bytes as kernel VM tunable values.

Six writable /proc/sys/vm parameters each carry one byte of payload.
The data lives entirely as kernel memory-management state -- no files,
no sockets, no heap allocations. The values survive process restart and
persist until a reboot or an explicit delete restores the originals.

Capacity is exactly 6 bytes: one per parameter listed in _PARAMS.
A second write overwrites the first (all six params are re-encoded).

Requires: Linux with write access to /proc/sys/vm (root or CAP_SYS_ADMIN).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

if sys.platform != "linux":
    raise NotImplementedError("dram_refresh_adapter requires Linux")

# (path, max_value_to_encode_255_safely)
# Values above max_val clamp to max_val when encoding, and are truncated on
# decode -- callers must keep chunks within the 6-byte limit.
_PARAMS: list[tuple[str, int]] = [
    ("/proc/sys/vm/swappiness",              200),
    ("/proc/sys/vm/dirty_ratio",             100),
    ("/proc/sys/vm/dirty_background_ratio",  100),
    ("/proc/sys/vm/vfs_cache_pressure",     1000),
    ("/proc/sys/vm/overcommit_ratio",        100),
    ("/proc/sys/vm/page_cluster",            255),
]

_MAX_BYTES = len(_PARAMS)


def _read_param(path: str) -> int:
    return int(Path(path).read_text().strip())


def _write_param(path: str, value: int) -> None:
    Path(path).write_text(str(value))


class DramRefreshAdapter(Adapter):
    """Scatter up to 6 bytes into kernel VM sysctl parameters."""

    def __init__(self) -> None:
        self._registry: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="dram-refresh",
            max_chunk_bytes=_MAX_BYTES,
            persistent=True,
            ordered=False,
            rate_limit_per_min=10,
            tags={
                "substrate": "kernel-vm-params",
                "requires": "Linux,root,CAP_SYS_ADMIN",
                "capacity": "6 bytes",
            },
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        data = encoded_chunk.encode()
        if len(data) > _MAX_BYTES:
            raise ValueError(
                f"chunk too large: {len(data)} bytes > {_MAX_BYTES} byte capacity"
            )

        padded = data.ljust(_MAX_BYTES, b"\x00")

        originals: list[int] = []
        try:
            for path, _ in _PARAMS:
                originals.append(_read_param(path))
        except OSError as exc:
            raise PermissionError(f"cannot read sysctl params: {exc}") from exc

        try:
            for (path, max_val), byte_val in zip(_PARAMS, padded):
                _write_param(path, min(byte_val, max_val))
        except OSError as exc:
            raise PermissionError(f"cannot write sysctl params: {exc}") from exc

        try:
            manifest_id = Chunk.decode(encoded_chunk).manifest_id
        except Exception:
            manifest_id = ""

        location = json.dumps({"originals": originals}, separators=(",", ":"))
        self._registry[location] = manifest_id
        return StoredChunkRef(adapter_name="dram-refresh", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location not in self._registry:
            raise KeyError(f"chunk not found: {ref.location[:60]}")
        try:
            raw = bytes(_read_param(path) for path, _ in _PARAMS)
        except OSError as exc:
            raise KeyError(f"cannot read sysctl params: {exc}") from exc
        return raw.rstrip(b"\x00").decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        if ref.location not in self._registry:
            return False
        try:
            loc = json.loads(ref.location)
            for (path, _), orig in zip(_PARAMS, loc["originals"]):
                _write_param(path, orig)
        except Exception:
            pass
        del self._registry[ref.location]
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="dram-refresh", location=loc)
            for loc, mid in self._registry.items()
            if mid == manifest_id
        ]

    async def health_check(self) -> bool:
        return Path(_PARAMS[0][0]).exists()

    async def close(self) -> None:
        pass

    async def capacity(self) -> dict[str, int | None]:
        return {
            "used_bytes": _MAX_BYTES if self._registry else 0,
            "free_bytes": 0 if self._registry else _MAX_BYTES,
            "total_bytes": _MAX_BYTES,
            "chunk_count": len(self._registry),
        }
