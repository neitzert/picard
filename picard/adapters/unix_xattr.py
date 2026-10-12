"""Unix extended attributes adapter — stores chunks in xattr.

Data stored in extended attributes is not shown by ``ls``, ``find``,
or ``du`` by default; use ``getfattr``/``xattr`` to enumerate it.
Works on any filesystem that supports xattr (ext4, xfs, btrfs, tmpfs, NFS…).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class UnixXattrAdapter(Adapter):
    """Scatter chunks into filesystem extended attributes."""

    def __init__(
        self,
        target_dir: str | Path = "/var/tmp/.picard",
        *,
        attr_prefix: str = "user.picard",
        max_chunk_bytes: int = 65_536,
    ) -> None:
        self._dir = Path(target_dir)
        self._prefix = attr_prefix
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="unix-xattr",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=1000,
        )

    def _chunk_key(self, encoded_chunk: str) -> str:
        from picard.chunker import Chunk

        try:
            chunk = Chunk.decode(encoded_chunk)
            return f"{chunk.manifest_id}_{chunk.block_index}_{chunk.chunk_index}"
        except (ValueError, Exception):
            return uuid.uuid4().hex

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        key = self._chunk_key(encoded_chunk)
        file_path = self._dir / key

        def _write():
            self._dir.mkdir(parents=True, exist_ok=True)
            file_path.touch()
            attr_name = f"{self._prefix}.data"
            os.setxattr(str(file_path), attr_name, encoded_chunk.encode())

        await asyncio.to_thread(_write)
        location = f"xattr://{self._dir}/{key}"
        return StoredChunkRef(adapter_name="unix-xattr", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        key = ref.location.rsplit("/", 1)[-1]
        file_path = self._dir / key

        def _read():
            attr_name = f"{self._prefix}.data"
            return os.getxattr(str(file_path), attr_name).decode()

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        key = ref.location.rsplit("/", 1)[-1]
        file_path = self._dir / key

        def _delete():
            try:
                file_path.unlink()
                return True
            except FileNotFoundError:
                return False

        return await asyncio.to_thread(_delete)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        def _list():
            refs: list[StoredChunkRef] = []
            if not self._dir.is_dir():
                return refs
            for f in self._dir.iterdir():
                if f.name.startswith(manifest_id):
                    loc = f"xattr://{self._dir}/{f.name}"
                    refs.append(
                        StoredChunkRef(adapter_name="unix-xattr", location=loc)
                    )
            return refs

        return await asyncio.to_thread(_list)

    async def health_check(self) -> bool:
        def _check():
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
                test = self._dir / ".xattr_test"
                test.touch()
                os.setxattr(str(test), "user.picard.test", b"1")
                os.removexattr(str(test), "user.picard.test")
                test.unlink()
                return True
            except Exception:
                return False

        return await asyncio.to_thread(_check)

    async def capacity(self) -> dict[str, int | None]:
        import shutil

        try:
            usage = shutil.disk_usage(self._dir)
            count = sum(1 for _ in self._dir.iterdir()) if self._dir.is_dir() else 0
            return {
                "used_bytes": None,
                "free_bytes": usage.free,
                "total_bytes": usage.total,
                "chunk_count": count,
            }
        except Exception:
            return {
                "used_bytes": None,
                "free_bytes": None,
                "total_bytes": None,
                "chunk_count": None,
            }
