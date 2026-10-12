"""NFS adapter -- stores chunks on NFS-mounted filesystems.

NFS is transparent once mounted, so chunks are regular files under
the mount point.  Optionally uses extended attributes (xattr) for
a lower-overhead storage mode.

    {mount_path}/{manifest_id}/{block}_{chunk}.pchk

No extra dependencies -- uses pathlib and os.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from pathlib import Path
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class NfsAdapter(Adapter):
    """Scatter chunks onto an NFS-mounted directory."""

    def __init__(
        self,
        mount_path: str | Path,
        *,
        use_xattr: bool = False,
        max_chunk_bytes: int = 1_048_576,
    ) -> None:
        self._root = Path(mount_path)
        self._use_xattr = use_xattr
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="nfs",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=5000,
        )

    def _chunk_path(self, manifest_id: str, fname: str) -> Path:
        return self._root / manifest_id / fname

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            manifest_id = chunk.manifest_id
            fname = f"{chunk.block_index}_{chunk.chunk_index}.pchk"
        except ValueError:
            manifest_id = uuid.uuid4().hex
            fname = f"{uuid.uuid4().hex}.pchk"

        fpath = self._chunk_path(manifest_id, fname)
        loc = f"nfs://{self._root}/{manifest_id}/{fname}"

        def _write():
            fpath.parent.mkdir(parents=True, exist_ok=True)
            if self._use_xattr:
                marker = fpath.with_suffix(".xattr")
                marker.touch()
                os.setxattr(str(marker), "user.picard.chunk", encoded_chunk.encode())
            else:
                fpath.write_text(encoded_chunk, encoding="utf-8")

        await asyncio.to_thread(_write)
        return StoredChunkRef(adapter_name="nfs", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        rel = ref.location.removeprefix(f"nfs://{self._root}/")
        parts = rel.split("/", 1)
        fpath = self._root / parts[0] / parts[1]

        def _read():
            if self._use_xattr:
                marker = fpath.with_suffix(".xattr")
                data = os.getxattr(str(marker), "user.picard.chunk")
                return data.decode()
            return fpath.read_text(encoding="utf-8")

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        rel = ref.location.removeprefix(f"nfs://{self._root}/")
        parts = rel.split("/", 1)
        fpath = self._root / parts[0] / parts[1]

        def _delete():
            try:
                if self._use_xattr:
                    marker = fpath.with_suffix(".xattr")
                    marker.unlink()
                else:
                    fpath.unlink()
                return True
            except FileNotFoundError:
                return False

        return await asyncio.to_thread(_delete)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        manifest_dir = self._root / manifest_id

        def _list():
            refs: list[StoredChunkRef] = []
            if not manifest_dir.is_dir():
                return refs
            suffix = ".xattr" if self._use_xattr else ".pchk"
            for f in manifest_dir.iterdir():
                if f.suffix == suffix:
                    name = f.stem + ".pchk" if self._use_xattr else f.name
                    loc = f"nfs://{self._root}/{manifest_id}/{name}"
                    refs.append(StoredChunkRef(adapter_name="nfs", location=loc))
            return refs

        return await asyncio.to_thread(_list)

    async def health_check(self) -> bool:
        return self._root.is_dir() and os.access(self._root, os.W_OK)

    async def capacity(self) -> dict[str, int | None]:
        def _cap():
            usage = shutil.disk_usage(self._root)
            chunk_count = sum(1 for _ in self._root.rglob("*.pchk"))
            used = sum(f.stat().st_size for f in self._root.rglob("*.pchk"))
            return {
                "used_bytes": used,
                "free_bytes": usage.free,
                "total_bytes": usage.total,
                "chunk_count": chunk_count,
            }

        try:
            return await asyncio.to_thread(_cap)
        except Exception:
            return {
                "used_bytes": None,
                "free_bytes": None,
                "total_bytes": None,
                "chunk_count": None,
            }
