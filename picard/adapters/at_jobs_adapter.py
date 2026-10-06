"""at(1) jobs adapter — stores chunks as scheduled job scripts.

Hides data in at(1) job comments. Each chunk becomes a scheduled job
whose script body is a shell comment containing the encoded payload:
  echo "# PICARD:{chunk_id}:{data}" | at now + 999 hours

The job never actually runs anything useful — it just sits in the queue.

Requires: at daemon (atd) running on the host.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class AtJobsAdapter(Adapter):
    """Scatter chunks into at(1) scheduled jobs."""

    def __init__(
        self,
        *,
        max_chunk_bytes: int = 4096,
    ) -> None:
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}  # location -> encoded_chunk

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="at-jobs",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_id = uuid.uuid4().hex[:12]

        script = f"#!/bin/sh\n# PICARD:{chunk_id}:{encoded_chunk}\ntrue\n"

        proc = await asyncio.create_subprocess_exec(
            "at", "now", "+", "999", "hours",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate(script.encode())

        # at(1) prints the job number on stderr, e.g. "job 42 at ..."
        job_number = chunk_id  # fallback
        stderr_text = stderr.decode(errors="replace")
        for token in stderr_text.split():
            if token.isdigit():
                job_number = token
                break

        location = f"at://{job_number}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="at-jobs", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]

        # Try to recover from the at queue via at -c
        job_number = ref.location.removeprefix("at://")
        proc = await asyncio.create_subprocess_exec(
            "at", "-c", job_number,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()

        for line in stdout.decode(errors="replace").splitlines():
            line = line.strip()
            if line.startswith("# PICARD:"):
                parts = line.split(":", 2)
                if len(parts) >= 3:
                    return parts[2]

        raise KeyError(f"chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        job_number = ref.location.removeprefix("at://")
        try:
            proc = await asyncio.create_subprocess_exec(
                "atrm", job_number,
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
        for location, data in self._cache.items():
            try:
                chunk = Chunk.decode(data)
                if chunk.manifest_id == manifest_id:
                    refs.append(StoredChunkRef(adapter_name="at-jobs", location=location))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                "atq",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
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
