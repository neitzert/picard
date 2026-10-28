"""cgroup v2 adapter -- stores chunks in cgroup memory.max parameter files.

Each chunk is base64url-encoded and split into 8-character segments. Each
segment is written as a 64-bit integer to the memory.max file of a sibling
cgroup named picard/<token>-<idx>. Sibling cgroups avoid the sub-cgroup
delegation restrictions imposed by systemd on Ubuntu 22.04+.

Requires: Linux, root, cgroup v2 (/sys/fs/cgroup/).
"""

from __future__ import annotations

import base64
import os
import sys
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

if sys.platform != "linux":
    raise ImportError("cgroup-limits adapter requires Linux")

_ADAPTER_NAME = "cgroup-limits"
_CGROUP_CONTROLLERS = "/sys/fs/cgroup/cgroup.controllers"


def _seg_to_int(seg: str) -> int:
    return int.from_bytes(seg.encode().ljust(8, b"\x00"), "big")


def _int_to_seg(val: int) -> str:
    return val.to_bytes(8, "big").rstrip(b"\x00").decode()


def _ensure_memory_controller(cgroup_root: str) -> None:
    """Best-effort: enable memory controller at the picard cgroup level."""
    for ctrl_path in (
        "/sys/fs/cgroup/cgroup.subtree_control",
        f"{cgroup_root}/../cgroup.subtree_control",
    ):
        try:
            with open(os.path.normpath(ctrl_path), "w") as f:
                f.write("+memory")
        except OSError:
            pass


class CgroupLimitsAdapter(Adapter):
    """Store chunks across sibling cgroup v2 memory.max parameter files."""

    def __init__(
        self,
        *,
        cgroup_root: str = "/sys/fs/cgroup/picard",
        max_chunk_bytes: int = 65536,
    ) -> None:
        self._root = cgroup_root
        self._max = max_chunk_bytes
        self._registry: dict[str, tuple[str, str]] = {}  # token -> (encoded_chunk, manifest_id)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_ADAPTER_NAME,
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            tags={
                "substrate": "cgroup-v2-limits",
                "requires": "Linux,root,cgroup-v2",
            },
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        data = encoded_chunk.encode()
        if len(data) > self._max:
            raise ValueError(f"Chunk too large: {len(data)} bytes > max {self._max}")
        try:
            manifest_id = Chunk.decode(encoded_chunk).manifest_id
        except Exception:
            manifest_id = ""

        token = uuid.uuid4().hex[:16]

        # base64url-encode for safe 8-byte segment encoding
        b64 = base64.urlsafe_b64encode(data).decode()
        segments = [b64[i:i + 8] for i in range(0, len(b64), 8)]

        # Enable memory controller at root -> picard level (best-effort).
        _ensure_memory_controller(self._root)
        os.makedirs(self._root, exist_ok=True)

        # Use flat sibling cgroups: picard/<token>-<idx>
        # This avoids the sub-cgroup delegation ENOMEM on systemd-managed hosts.
        # <token>-len stores the total b64 length; <token>-<i> stores each segment.
        for suffix, value in [("len", len(b64))] + [(str(i), _seg_to_int(seg)) for i, seg in enumerate(segments)]:
            cg_dir = os.path.join(self._root, f"{token}-{suffix}")
            os.makedirs(cg_dir, exist_ok=True)
            with open(os.path.join(cg_dir, "memory.max"), "w") as f:
                f.write(str(value))

        self._registry[token] = (encoded_chunk, manifest_id)
        return StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"cgroup-limits://{token}")

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = ref.location.removeprefix("cgroup-limits://")
        if token not in self._registry:
            raise KeyError(f"cgroup-limits chunk not found: {token}")
        return self._registry[token][0]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = ref.location.removeprefix("cgroup-limits://")
        if token not in self._registry:
            return False
        if os.path.isdir(self._root):
            for entry in os.listdir(self._root):
                if entry.startswith(f"{token}-"):
                    try:
                        os.rmdir(os.path.join(self._root, entry))
                    except OSError:
                        pass
        del self._registry[token]
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"cgroup-limits://{tok}")
            for tok, (_, mid) in self._registry.items()
            if mid == manifest_id
        ]

    async def health_check(self) -> bool:
        if not os.path.isfile(_CGROUP_CONTROLLERS):
            return False
        parent = os.path.dirname(self._root) or "/sys/fs/cgroup"
        return os.access(parent, os.W_OK)

    async def close(self) -> None:
        pass
