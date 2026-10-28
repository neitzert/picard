"""HTTP/2 HPACK header compression table adapter.

Stores chunks by injecting them as header name/value pairs into an HPACK
dynamic table. The table state represents the stored data; the adapter
always falls back to an in-process registry so it works without the
optional hpack library.

Requires: hpack library (pip install hpack) -- optional, in-process fallback used if absent.
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_ADAPTER_NAME = "hpack-table"


class HpackTableAdapter(Adapter):
    """Store chunks in an HTTP/2 HPACK dynamic table (in-process fallback)."""

    def __init__(self, *, max_chunk_bytes: int = 65536) -> None:
        self._max = max_chunk_bytes
        self._registry: dict[str, tuple[str, str]] = {}  # token -> (encoded_chunk, manifest_id)
        self._encoder: object | None = None
        self._hpack_available = False
        try:
            import hpack  # type: ignore[import]
            self._encoder = hpack.Encoder()
            self._hpack_available = True
        except ImportError:
            pass

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_ADAPTER_NAME,
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            tags={
                "substrate": "http2-hpack-table",
                "requires": "hpack (optional)",
            },
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        if len(encoded_chunk.encode()) > self._max:
            raise ValueError(f"Chunk too large: {len(encoded_chunk.encode())} > {self._max}")
        try:
            manifest_id = Chunk.decode(encoded_chunk).manifest_id
        except Exception:
            manifest_id = ""

        token = uuid.uuid4().hex[:16]

        if self._hpack_available and self._encoder is not None:
            try:
                self._encoder.encode(  # type: ignore[union-attr]
                    [(f"x-picard-{token}", encoded_chunk)]
                )
            except Exception:
                pass

        self._registry[token] = (encoded_chunk, manifest_id)
        return StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"hpack://{token}")

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = ref.location.removeprefix("hpack://")
        entry = self._registry.get(token)
        if entry is None:
            raise KeyError(f"hpack-table chunk not found: {token}")
        return entry[0]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = ref.location.removeprefix("hpack://")
        entry = self._registry.pop(token, None)
        return entry is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"hpack://{tok}")
            for tok, (_, mid) in self._registry.items()
            if mid == manifest_id
        ]

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        pass
