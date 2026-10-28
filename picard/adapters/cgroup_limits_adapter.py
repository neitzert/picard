"""cgroup v2 adapter -- stores chunks in cgroup memory.max parameter files.

Each chunk is base64url-encoded, split into 8-character segments, and each
segment is written as a 64-bit integer to the memory.max file of a numbered
sub-cgroup. Data persists until the cgroup hierarchy is removed.

Requires: Linux, root, cgroup v2 (/sys/fs/cgroup/).
"""

from __future__ import annotations

import base64
import os
import sys
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


class CgroupLimitsAdapter(Adapter):
    """Store chunks in cgroup v2 memory.max parameter files."""

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

        import uuid
        token = uuid.uuid4().hex[:16]
        token_dir = os.path.join(self._root, token)

        # base64url-encode for safe 8-byte segment encoding
        b64 = base64.urlsafe_b64encode(data).decode()
        segments = [b64[i:i + 8] for i in range(0, len(b64), 8)]

        # Write length to parent cgroup memory.max, segments to sub-cgroups
        os.makedirs(token_dir, exist_ok=True)
        with open(os.path.join(token_dir, "memory.max"), "w") as f:
            f.write(str(len(b64)))

        for i, seg in enumerate(segments):
            sub_dir = os.path.join(token_dir, str(i))
            os.makedirs(sub_dir, exist_ok=True)
            with open(os.path.join(sub_dir, "memory.max"), "w") as f:
                f.write(str(_seg_to_int(seg)))

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
        token_dir = os.path.join(self._root, token)
        # Remove sub-cgroups first (numbered dirs), then parent
        if os.path.isdir(token_dir):
            for entry in sorted(os.listdir(token_dir)):
                sub = os.path.join(token_dir, entry)
                if os.path.isdir(sub):
                    try:
                        os.rmdir(sub)
                    except OSError:
                        pass
            try:
                os.rmdir(token_dir)
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
        parent = os.path.dirname(self._root)
        return os.access(parent, os.W_OK)

    async def close(self) -> None:
        pass
