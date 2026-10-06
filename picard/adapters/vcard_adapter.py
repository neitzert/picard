"""vCard adapter — stores chunks in vCard contact cards.

Chunks are embedded in the NOTE field of vCard 3.0 entries.

Pure in-memory — no CardDAV server required.
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class VCardAdapter(Adapter):
    """Scatter chunks into vCard contact entries."""

    def __init__(
        self,
        *,
        max_chunk_bytes: int = 4096,
    ) -> None:
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="vcard",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=True,
            rate_limit_per_min=120,
        )

    def _build_vcf(self) -> str:
        cards = ""
        for loc, data in self._cache.items():
            uid = loc.rsplit("/", 1)[-1]
            cards += (
                "BEGIN:VCARD\n"
                "VERSION:3.0\n"
                f"FN:PICARD Contact {uid}\n"
                f"UID:{uid}\n"
                f"NOTE:{data}\n"
                "END:VCARD\n"
            )
        return cards

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_id = uuid.uuid4().hex[:12]
        location = f"vcard://{chunk_id}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="vcard", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]
        raise KeyError(f"chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="vcard", location=loc)
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
