"""Git Config adapter — stores chunks in .gitconfig custom sections.

Stores data in git configuration custom sections. Each chunk becomes a
key-value pair under a configurable section (default ``[picard]``).

Requires: git CLI available on PATH.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class GitConfigAdapter(Adapter):
    """Scatter chunks into git config custom sections."""

    def __init__(
        self,
        *,
        config_path: str | None = None,
        section: str = "picard",
        max_chunk_bytes: int = 8192,
    ) -> None:
        self._config_path = config_path
        self._section = section
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="gitconfig",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=120,
        )

    def _scope_args(self) -> list[str]:
        """Return git config scope arguments."""
        if self._config_path:
            return ["--file", self._config_path]
        return ["--global"]

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_id = uuid.uuid4().hex[:12]
        key = f"{self._section}.chunk-{chunk_id}"

        cmd = ["git", "config", *self._scope_args(), key, encoded_chunk]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"git config set failed: {stderr.decode().strip()}")

        location = f"gitconfig://{self._section}.chunk-{chunk_id}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="gitconfig", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]

        # Extract key from location: gitconfig://section.key
        key = ref.location.replace("gitconfig://", "", 1)

        cmd = ["git", "config", *self._scope_args(), "--get", key]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            raise KeyError(f"chunk not found: {ref.location}")

        data = stdout.decode().strip()
        self._cache[ref.location] = data
        return data

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        key = ref.location.replace("gitconfig://", "", 1)

        try:
            cmd = ["git", "config", *self._scope_args(), "--unset", key]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
        except Exception:
            pass

        existed = ref.location in self._cache
        self._cache.pop(ref.location, None)
        return existed

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        for loc, data in self._cache.items():
            try:
                chunk = Chunk.decode(data)
                if chunk.manifest_id == manifest_id:
                    refs.append(StoredChunkRef(adapter_name="gitconfig", location=loc))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "--version",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()
            return proc.returncode == 0
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
