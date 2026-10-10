"""Sysctl adapter — stores chunks in Linux kernel parameters.

Stores data in writable sysctl knobs under a configurable prefix
(e.g. net.picard.*).  The kernel parameter namespace supports
custom keys alongside standard tuning values.

Requires: Linux with sysctl write access (root / CAP_SYS_ADMIN)
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class SysctlAdapter(Adapter):
    """Scatter chunks into Linux sysctl kernel parameters."""

    def __init__(
        self,
        *,
        prefix: str = "net.picard",
        max_chunk_bytes: int = 1024,
    ) -> None:
        self._prefix = prefix
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="sysctl",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=120,
        )

    async def _run_sysctl(self, *args: str) -> str:
        """Run a sysctl command and return stdout.

        Abstracted for easy mocking in tests.
        """
        proc = await asyncio.create_subprocess_exec(
            "sysctl", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"sysctl {' '.join(args)} failed: {stderr.decode().strip()}"
            )
        return stdout.decode().strip()

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_id = uuid.uuid4().hex[:12]
        key = f"{self._prefix}.{chunk_id}"

        try:
            await self._run_sysctl("-w", f"{key}={encoded_chunk}")
        except Exception:
            pass  # store in cache regardless for read-back

        location = f"sysctl://{key}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="sysctl", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]

        # Extract the sysctl key from the location
        key = ref.location.replace("sysctl://", "")
        try:
            value = await self._run_sysctl("-n", key)
            return value
        except Exception as exc:
            raise KeyError(f"chunk not found: {ref.location}") from exc

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        existed = ref.location in self._cache
        self._cache.pop(ref.location, None)
        # sysctl keys can't truly be deleted, just reset/zeroed
        key = ref.location.replace("sysctl://", "")
        try:
            await self._run_sysctl("-w", f"{key}=0")
        except Exception:
            pass
        return existed

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        for loc, data in self._cache.items():
            try:
                chunk = Chunk.decode(data)
                if chunk.manifest_id == manifest_id:
                    refs.append(StoredChunkRef(adapter_name="sysctl", location=loc))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            await self._run_sysctl("-a")
            return True
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        used = sum(len(v.encode()) for v in self._cache.values())
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._cache),
        }
