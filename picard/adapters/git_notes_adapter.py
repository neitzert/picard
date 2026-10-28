"""Git notes adapter -- stores chunks as git note objects on anonymous blobs.

Each chunk is written as a git note attached to an empty blob object. Notes
live in refs/notes/<ref> (default: refs/notes/picard) and are invisible to
normal git log unless --notes=picard is passed. The blob they attach to is
unreferenced by any commit, so notes do not affect history at all.

Requires: git CLI on PATH. Must be used inside a git repository (or pass
repo_path pointing to one). No elevated privileges needed.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_ADAPTER_NAME = "git-notes"


class GitNotesAdapter(Adapter):
    """Store chunks as git note objects on anonymous blobs."""

    def __init__(
        self,
        *,
        repo_path: str = ".",
        ref: str = "picard",
        max_chunk_bytes: int = 65536,
    ) -> None:
        self._repo_path = repo_path
        self._ref = ref
        self._max = max_chunk_bytes
        self._cache: dict[str, tuple[str, str]] = {}  # blob_sha -> (encoded_chunk, manifest_id)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_ADAPTER_NAME,
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=120,
            tags={
                "substrate": "git-notes",
                "requires": "git",
            },
        )

    async def _git(self, *args: str, stdin: bytes | None = None) -> str:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._repo_path,
        )
        stdout, stderr = await proc.communicate(input=stdin)
        if proc.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args)} failed: {stderr.decode().strip()}"
            )
        return stdout.decode().strip()

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        if len(encoded_chunk.encode()) > self._max:
            raise ValueError(
                f"Chunk too large: {len(encoded_chunk.encode())} > {self._max}"
            )
        try:
            manifest_id = Chunk.decode(encoded_chunk).manifest_id
        except Exception:
            manifest_id = ""

        blob_sha = await self._git("hash-object", "-w", "--stdin", stdin=b"")
        await self._git(
            "notes", f"--ref={self._ref}", "add",
            "-m", encoded_chunk, blob_sha,
        )
        self._cache[blob_sha] = (encoded_chunk, manifest_id)
        return StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"git-notes://{blob_sha}")

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        blob_sha = ref.location.removeprefix("git-notes://")
        if blob_sha in self._cache:
            return self._cache[blob_sha][0]
        try:
            data = await self._git("notes", f"--ref={self._ref}", "show", blob_sha)
            return data
        except RuntimeError as exc:
            raise KeyError(f"chunk not found: {ref.location}") from exc

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        blob_sha = ref.location.removeprefix("git-notes://")
        try:
            await self._git("notes", f"--ref={self._ref}", "remove", blob_sha)
        except RuntimeError:
            pass
        existed = blob_sha in self._cache
        self._cache.pop(blob_sha, None)
        return existed

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        try:
            output = await self._git("notes", f"--ref={self._ref}", "list")
        except RuntimeError:
            return refs

        for line in output.splitlines():
            parts = line.strip().split()
            if len(parts) < 2:
                continue
            blob_sha = parts[1]
            if blob_sha in self._cache:
                if self._cache[blob_sha][1] == manifest_id:
                    refs.append(StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"git-notes://{blob_sha}"))
            else:
                try:
                    data = await self._git("notes", f"--ref={self._ref}", "show", blob_sha)
                    mid = Chunk.decode(data).manifest_id
                    self._cache[blob_sha] = (data, mid)
                    if mid == manifest_id:
                        refs.append(StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"git-notes://{blob_sha}"))
                except Exception:
                    continue
        return refs

    async def health_check(self) -> bool:
        try:
            await self._git("--version")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        pass
