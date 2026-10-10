"""SSH known_hosts adapter — stores chunks as placeholder known_hosts entries.

Encodes chunk data into the hostname and key-comment fields of synthetic
host key records appended to a known_hosts file.

Only use this on files you own.  The placeholder key material is
syntactically valid but non-functional.

Requires: nothing beyond stdlib (file I/O only)
"""

from __future__ import annotations

import os
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


_PLACEHOLDER_KEY = (
    "AAAAB3NzaC1yc2EAAAADAQABAAABgQC7vbqajDRETmoysn0"
    "WMhJkBb3ztGnQjFTEEIxqV0mx05iFBNhLEz0wVPz4ixPvn"
    "qOkEHMFNhResKPo3FpiLsaa0persistent1keydata0filler"
)


class KnownHostsAdapter(Adapter):
    """Scatter chunks into SSH known_hosts placeholder entries."""

    def __init__(
        self,
        *,
        hosts_path: str = "~/.ssh/known_hosts",
        max_chunk_bytes: int = 4096,
    ) -> None:
        self._hosts_path = os.path.expanduser(hosts_path)
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    # -- ABC ----------------------------------------------------------

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="known-hosts",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=120,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_id = uuid.uuid4().hex[:12]
        hostname = f"{chunk_id}.picard.local"
        line = f"{hostname} ssh-rsa {_PLACEHOLDER_KEY} PICARD:{chunk_id}:{encoded_chunk}\n"

        try:
            os.makedirs(os.path.dirname(self._hosts_path), exist_ok=True)
            with open(self._hosts_path, "a") as fh:
                fh.write(line)
        except OSError:
            pass  # real write failed; cache still provides read-back

        location = f"known-hosts://{chunk_id}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="known-hosts", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]

        chunk_id = ref.location.split("://", 1)[-1]
        marker = f"PICARD:{chunk_id}:"

        try:
            with open(self._hosts_path) as fh:
                for line in fh:
                    if marker in line:
                        # comment is everything after the marker prefix
                        idx = line.index(marker)
                        return line[idx + len(marker):].rstrip("\n")
        except OSError:
            pass

        raise KeyError(f"chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        existed = ref.location in self._cache
        self._cache.pop(ref.location, None)

        chunk_id = ref.location.split("://", 1)[-1]
        marker = f"PICARD:{chunk_id}:"
        try:
            with open(self._hosts_path) as fh:
                lines = fh.readlines()
            with open(self._hosts_path, "w") as fh:
                for line in lines:
                    if marker not in line:
                        fh.write(line)
        except OSError:
            pass

        return existed

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        for loc, data in self._cache.items():
            try:
                chunk = Chunk.decode(data)
                if chunk.manifest_id == manifest_id:
                    refs.append(StoredChunkRef(adapter_name="known-hosts", location=loc))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            parent = os.path.dirname(self._hosts_path)
            return os.path.isdir(parent)
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
