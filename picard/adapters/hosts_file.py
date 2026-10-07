"""Hosts file adapter — stores chunks encoded as hostnames.

Chunk data is encoded into hostname-safe format (hex) and
written as entries in /etc/hosts (or equivalent) format.
Each entry maps a 127.x.y.z IP to a hex-encoded hostname.

Supports file persistence when file_path is provided.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_MARKER = "# PICARD:"


class HostsFileAdapter(Adapter):
    """Scatter chunks as hostname entries in a hosts file."""

    def __init__(
        self,
        *,
        file_path: str | None = None,
        max_chunk_bytes: int = 255,
    ) -> None:
        self._max = max_chunk_bytes
        self._file_path = Path(file_path) if file_path else None
        self._cache: dict[str, str] = {}
        self._counter = 1
        if self._file_path and self._file_path.exists():
            self._load_from_file()

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="hosts_file",
            max_chunk_bytes=self._max,
            persistent=self._file_path is not None,
            ordered=True,
            rate_limit_per_min=120,
        )

    @staticmethod
    def _encode_hostname(data: str) -> str:
        hexed = data.encode().hex()
        parts = [hexed[i:i+62] for i in range(0, len(hexed), 62)]
        return ".".join(parts) + ".picard.local"

    @staticmethod
    def _decode_hostname(hostname: str) -> str:
        stripped = hostname.replace(".picard.local", "")
        hexed = stripped.replace(".", "")
        return bytes.fromhex(hexed).decode()

    def _build_hosts(self) -> str:
        lines = ["# PICARD hosts file entries\n"]
        for idx, (loc, data) in enumerate(self._cache.items(), start=1):
            chunk_id = loc.split("://")[1]
            hostname = self._encode_hostname(data)
            ip = f"127.0.{idx // 256}.{idx % 256}"
            lines.append(f"{ip}\t{hostname} {_MARKER}{chunk_id}\n")
        return "".join(lines)

    def _flush(self) -> None:
        if self._file_path is None:
            return
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        self._file_path.write_text(self._build_hosts(), encoding="utf-8")

    def _load_from_file(self) -> None:
        try:
            raw = self._file_path.read_text(encoding="utf-8")
            for line in raw.splitlines():
                if _MARKER not in line:
                    continue
                parts = line.split(_MARKER, 1)
                chunk_id = parts[1].strip()
                hostname_part = parts[0].strip().split("\t", 1)
                if len(hostname_part) < 2:
                    continue
                hostname = hostname_part[1].strip()
                try:
                    data = self._decode_hostname(hostname)
                    loc = f"hosts://{chunk_id}"
                    self._cache[loc] = data
                except Exception:
                    pass
        except Exception:
            pass

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_id = uuid.uuid4().hex[:12]
        location = f"hosts://{chunk_id}"
        self._cache[location] = encoded_chunk
        self._counter += 1
        self._flush()
        return StoredChunkRef(adapter_name="hosts_file", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]
        if self._file_path and self._file_path.exists():
            self._load_from_file()
            if ref.location in self._cache:
                return self._cache[ref.location]
        raise KeyError(f"chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        existed = self._cache.pop(ref.location, None) is not None
        if existed:
            self._flush()
        return existed

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="hosts_file", location=loc)
            for loc in self._cache
        ]

    async def health_check(self) -> bool:
        if self._file_path:
            return self._file_path.parent.exists()
        return True

    async def capacity(self) -> dict[str, int | None]:
        used = sum(len(v.encode()) for v in self._cache.values())
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._cache),
        }
