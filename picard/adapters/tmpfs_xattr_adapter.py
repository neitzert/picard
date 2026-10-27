"""tmpfs xattr adapter -- stores chunks as xattrs on a single sentinel file.

All chunks share one sentinel file in /dev/shm. Each chunk occupies a
distinct extended attribute named ``user.picard.<token>``. The sentinel
file itself holds no payload data; the xattrs are the storage medium.

tmpfs on Linux supports xattrs with no additional mount options. The
xattr namespace limit is 65535 bytes per value, matching the chunk cap.

Requires: Linux (os.setxattr / os.getxattr are Linux-only).
"""

from __future__ import annotations

import os
import sys
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

if sys.platform != "linux":
    raise NotImplementedError("tmpfs_xattr requires Linux")

_PREFIX = "user.picard."


class TmpfsXattrAdapter(Adapter):
    """Scatter chunks as xattrs on a single tmpfs sentinel file."""

    def __init__(
        self,
        *,
        sentinel_file: str = "/dev/shm/.picard_xattr_store",
        max_chunk_bytes: int = 65535,
    ) -> None:
        self._sentinel = sentinel_file
        self._max = max_chunk_bytes
        open(self._sentinel, "a").close()

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="tmpfs-xattr",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=2000,
            tags={
                "substrate": "tmpfs-xattr",
                "requires": "Linux,tmpfs",
            },
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        os.setxattr(self._sentinel, f"{_PREFIX}{token}", encoded_chunk.encode())
        return StoredChunkRef(adapter_name="tmpfs-xattr", location=token)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        try:
            return os.getxattr(self._sentinel, f"{_PREFIX}{ref.location}").decode()
        except OSError as exc:
            raise KeyError(f"chunk not found: {ref.location}") from exc

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        try:
            os.removexattr(self._sentinel, f"{_PREFIX}{ref.location}")
            return True
        except OSError:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        try:
            keys = os.listxattr(self._sentinel)
        except OSError:
            return refs
        for key in keys:
            if not key.startswith(_PREFIX):
                continue
            token = key[len(_PREFIX):]
            try:
                raw = os.getxattr(self._sentinel, key).decode()
                if Chunk.decode(raw).manifest_id == manifest_id:
                    refs.append(StoredChunkRef(adapter_name="tmpfs-xattr", location=token))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        test_key = f"{_PREFIX}_health_probe_{uuid.uuid4().hex[:8]}"
        try:
            os.setxattr(self._sentinel, test_key, b"1")
            os.getxattr(self._sentinel, test_key)
            os.removexattr(self._sentinel, test_key)
            return True
        except Exception:
            return False

    async def close(self) -> None:
        pass

    async def capacity(self) -> dict[str, int | None]:
        try:
            keys = [k for k in os.listxattr(self._sentinel) if k.startswith(_PREFIX)]
            used = sum(
                len(os.getxattr(self._sentinel, k)) for k in keys
            )
            return {
                "used_bytes": used,
                "free_bytes": None,
                "total_bytes": None,
                "chunk_count": len(keys),
            }
        except Exception:
            return {"used_bytes": None, "free_bytes": None, "total_bytes": None, "chunk_count": None}
