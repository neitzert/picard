"""Git protocol adapter — stores chunks as git blobs in a bare repository.

Each chunk becomes a git blob object, referenced by SHA:
    git://{repo}/{sha}

Requires: git CLI available on PATH
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class GitProtocolAdapter(Adapter):
    """Scatter chunks into git blob objects in a repository."""

    def __init__(
        self,
        repo_path: str,
        *,
        branch: str = "picard/chunks",
        max_chunk_bytes: int = 1_048_576,
    ) -> None:
        self._repo = repo_path
        self._branch = branch
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="git-protocol",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=500,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            self._repo,
            "hash-object",
            "-w",
            "--stdin",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(encoded_chunk.encode())
        if proc.returncode != 0:
            raise RuntimeError(f"git hash-object failed: {stderr.decode()}")

        sha = stdout.decode().strip()

        try:
            chunk = Chunk.decode(encoded_chunk)
            note = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
        except ValueError:
            note = uuid.uuid4().hex

        proc2 = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            self._repo,
            "notes",
            "--ref",
            self._branch,
            "add",
            "-f",
            "-m",
            note,
            sha,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc2.communicate()

        location = f"git://{self._repo}/{sha}"
        return StoredChunkRef(adapter_name="git-protocol", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        sha = ref.location.rsplit("/", 1)[1]
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            self._repo,
            "cat-file",
            "blob",
            sha,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise KeyError(f"blob not found: {sha}")
        return stdout.decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        sha = ref.location.rsplit("/", 1)[1]
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            self._repo,
            "notes",
            "--ref",
            self._branch,
            "remove",
            sha,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return proc.returncode == 0

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            self._repo,
            "notes",
            "--ref",
            self._branch,
            "list",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return []

        refs = []
        for line in stdout.decode().strip().splitlines():
            parts = line.split()
            if len(parts) >= 2:
                blob_sha = parts[1]
                note_proc = await asyncio.create_subprocess_exec(
                    "git",
                    "-C",
                    self._repo,
                    "notes",
                    "--ref",
                    self._branch,
                    "show",
                    blob_sha,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                note_out, _ = await note_proc.communicate()
                note_text = note_out.decode().strip()
                if note_text.startswith(manifest_id):
                    refs.append(
                        StoredChunkRef(
                            adapter_name="git-protocol",
                            location=f"git://{self._repo}/{blob_sha}",
                        )
                    )
        return refs

    async def health_check(self) -> bool:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            self._repo,
            "rev-parse",
            "--git-dir",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return proc.returncode == 0
