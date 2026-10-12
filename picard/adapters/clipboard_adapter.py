"""Clipboard adapter — stores chunks via the system clipboard.

Since the clipboard is a single-slot resource, this adapter
maintains an internal cache and serialises all chunks into a
single clipboard payload (JSON-encoded dict).

Pure in-memory cache with clipboard as the backing store.
"""

from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class ClipboardAdapter(Adapter):
    """Scatter chunks into the system clipboard."""

    def __init__(
        self,
        *,
        max_chunk_bytes: int = 4096,
    ) -> None:
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="clipboard",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=60,
        )

    def _sync_to_clipboard(self) -> None:
        """Attempt to push cache to system clipboard (best-effort)."""
        try:
            import subprocess
            payload = json.dumps(self._cache)
            # Try platform-appropriate clipboard commands
            try:
                proc = subprocess.Popen(
                    ["clip.exe"],
                    stdin=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                proc.communicate(input=payload.encode(), timeout=2)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                try:
                    proc = subprocess.Popen(
                        ["xclip", "-selection", "clipboard"],
                        stdin=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                    )
                    proc.communicate(input=payload.encode(), timeout=2)
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    pass
        except Exception:
            pass  # clipboard is best-effort

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_id = uuid.uuid4().hex[:12]
        location = f"clipboard://{chunk_id}"
        self._cache[location] = encoded_chunk
        self._sync_to_clipboard()
        return StoredChunkRef(adapter_name="clipboard", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]
        raise KeyError(f"chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        removed = self._cache.pop(ref.location, None) is not None
        if removed:
            self._sync_to_clipboard()
        return removed

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="clipboard", location=loc)
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
