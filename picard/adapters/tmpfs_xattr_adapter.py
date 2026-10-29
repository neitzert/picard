"""tmpfs xattr adapter -- stores chunks as extended attributes on /dev/shm files.

Each write_chunk creates an empty file in /dev/shm/ and sets
user.picard.chunk = encoded_chunk via os.setxattr(). Cold reads use
os.getxattr(). Visible via 'getfattr -d /dev/shm/<token>'.

Linux 3.5+. No root required (tmpfs xattr enabled by default on Ubuntu).
"""
from __future__ import annotations

import sys
if sys.platform != "linux":
    raise ImportError("tmpfs_xattr adapter requires Linux")

import asyncio
import json
import os
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "tmpfs_xattr"
_XATTR_KEY = b"user.picard.chunk"


class TmpfsXattrAdapter(Adapter):

    def __init__(self, *, store_dir: str = "/dev/shm") -> None:
        self._dir = store_dir
        self._reg: dict[str, tuple[str, str]] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            tags={"requires": "Linux", "substrate": "tmpfs-extended-attribute"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            token = uuid.uuid4().hex
            path = os.path.join(self._dir, f"picard.{token}")
            with open(path, "wb"):
                pass
            os.setxattr(path, _XATTR_KEY, encoded_chunk.encode("utf-8"))
            return token, path

        token, path = await asyncio.to_thread(_write)
        self._reg[token] = (path, encoded_chunk)
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token, "path": path}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token][1]

        def _read():
            return os.getxattr(loc["path"], _XATTR_KEY).decode("utf-8")

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        token = loc["token"]
        self._reg.pop(token, None)
        def _del():
            if os.path.exists(loc["path"]):
                os.unlink(loc["path"])
        await asyncio.to_thread(_del)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        from picard.chunker import Chunk
        refs = []
        for token, (path, data) in self._reg.items():
            try:
                if Chunk.decode(data).manifest_id == manifest_id:
                    refs.append(StoredChunkRef(adapter_name=_NAME,
                                               location=json.dumps({"token": token,
                                                "path": path})))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            test_path = os.path.join(self._dir, f"picard.hc.{uuid.uuid4().hex[:8]}")
            with open(test_path, "wb"):
                pass
            os.setxattr(test_path, _XATTR_KEY, b"test")
            val = os.getxattr(test_path, _XATTR_KEY)
            os.unlink(test_path)
            return val == b"test"
        except Exception:
            return False

    async def close(self) -> None:
        pass
