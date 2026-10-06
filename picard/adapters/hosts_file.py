"""Hosts file adapter — stores chunks encoded as hostnames.

Chunk data is encoded into hostname-safe format (hex) and
written as entries in /etc/hosts (or equivalent) format.
Each entry maps a 127.x.y.z IP to a hex-encoded hostname.

In-memory by default — actual hosts file writes are
gated behind an explicit file path.
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class HostsFileAdapter(Adapter):
    """Scatter chunks as hostname entries in a hosts file."""

    def __init__(
        self,
        *,
        max_chunk_bytes: int = 255,  # DNS label limit
    ) -> None:
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}
        self._counter = 1

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="hosts_file",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=True,
            rate_limit_per_min=120,
        )

    @staticmethod
    def _encode_hostname(data: str) -> str:
        """Encode data as a hostname-safe hex string with dots every 63 chars."""
        hexed = data.encode().hex()
        # DNS labels max 63 chars, so split into segments
        parts = [hexed[i:i+62] for i in range(0, len(hexed), 62)]
        return ".".join(parts) + ".picard.local"

    @staticmethod
    def _decode_hostname(hostname: str) -> str:
        """Decode a hostname back to the original data."""
        # Remove .picard.local suffix
        stripped = hostname.replace(".picard.local", "")
        hexed = stripped.replace(".", "")
        return bytes.fromhex(hexed).decode()

    def _build_hosts(self) -> str:
        lines = ["# PICARD hosts file entries\n"]
        for idx, (loc, data) in enumerate(self._cache.items(), start=1):
            hostname = self._encode_hostname(data)
            ip = f"127.0.{idx // 256}.{idx % 256}"
            lines.append(f"{ip}\t{hostname}\n")
        return "".join(lines)

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_id = uuid.uuid4().hex[:12]
        location = f"hosts://{chunk_id}"
        self._cache[location] = encoded_chunk
        self._counter += 1
        return StoredChunkRef(adapter_name="hosts_file", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]
        raise KeyError(f"chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="hosts_file", location=loc)
            for loc in self._cache
        ]

    async def health_check(self) -> bool:
        return True

    async def capacity(self) -> dict[str, int | None]:
        used = sum(len(v.encode()) for v in self._cache.values())
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._cache),
        }
