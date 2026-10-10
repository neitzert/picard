"""Crontab adapter — stores chunks in crontab comment fields.

Each chunk becomes a crontab entry with the data in a comment:
  # PICARD:<chunk_id>:<encoded_data>
  * * * * * /bin/true

The job itself is a harmless no-op; the payload rides in the comment.

Requires: crontab command-line tool
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class CrontabAdapter(Adapter):
    """Scatter chunks into crontab entries as comments."""

    def __init__(
        self,
        *,
        user: str | None = None,
        max_chunk_bytes: int = 4096,
    ) -> None:
        self._user = user
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="crontab",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    async def _run_crontab(self, *args: str, stdin: str | None = None) -> str:
        """Run a crontab command and return stdout.

        Abstracted for easy mocking in tests.
        """
        cmd = ["crontab"]
        if self._user:
            cmd.extend(["-u", self._user])
        cmd.extend(args)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if stdin else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(
            input=stdin.encode() if stdin else None,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"crontab failed: {stderr.decode().strip()}"
            )
        return stdout.decode().strip()

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_id = uuid.uuid4().hex[:12]
        user_label = self._user or "current"

        # Build the crontab entry
        entry = f"# PICARD:{chunk_id}:{encoded_chunk}\n* * * * * /bin/true"

        # Try to append to existing crontab
        try:
            existing = await self._run_crontab("-l")
        except Exception:
            existing = ""

        new_crontab = f"{existing}\n{entry}\n" if existing else f"{entry}\n"

        try:
            await self._run_crontab("-", stdin=new_crontab)
        except Exception:
            pass  # store in cache regardless

        location = f"crontab://{user_label}/{chunk_id}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="crontab", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]

        # Extract chunk_id from location
        chunk_id = ref.location.rsplit("/", 1)[-1]
        marker = f"PICARD:{chunk_id}:"

        try:
            output = await self._run_crontab("-l")
            for line in output.splitlines():
                if line.startswith("# ") and marker in line:
                    # Format: # PICARD:<chunk_id>:<data>
                    _, rest = line.split(marker, 1)
                    return rest
        except Exception:
            pass

        raise KeyError(f"chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        existed = ref.location in self._cache
        self._cache.pop(ref.location, None)

        # Try to remove from live crontab
        chunk_id = ref.location.rsplit("/", 1)[-1]
        marker = f"PICARD:{chunk_id}:"

        try:
            output = await self._run_crontab("-l")
            lines = output.splitlines()
            filtered: list[str] = []
            skip_next = False
            for line in lines:
                if skip_next:
                    skip_next = False
                    continue
                if line.startswith("# ") and marker in line:
                    skip_next = True  # skip the associated cron job line too
                    continue
                filtered.append(line)
            new_crontab = "\n".join(filtered) + "\n"
            await self._run_crontab("-", stdin=new_crontab)
        except Exception:
            pass

        return existed

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        for loc, data in self._cache.items():
            try:
                chunk = Chunk.decode(data)
                if chunk.manifest_id == manifest_id:
                    refs.append(StoredChunkRef(adapter_name="crontab", location=loc))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            await self._run_crontab("-l")
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
