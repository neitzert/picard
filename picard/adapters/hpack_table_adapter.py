"""HPACK dynamic table adapter -- stores chunks in HTTP/2 header compression.

Each write_chunk adds a 'x-picard-<token>: <encoded_chunk>' entry to an
HPACK encoder's dynamic table via hpack.Encoder. The dynamic table is the
substrate; data lives in the in-process encoder state.

Requires: pip install hpack
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "hpack_table"


class HpackTableAdapter(Adapter):

    def __init__(self) -> None:
        self._reg: dict[str, str] = {}  # token -> encoded_chunk

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=False,
            ordered=False,
            tags={"requires": "hpack", "substrate": "http2-hpack-dynamic-table"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            try:
                import hpack
            except ImportError as e:
                raise ImportError("hpack required: pip install hpack") from e
            token = uuid.uuid4().hex[:16]
            encoder = hpack.Encoder()
            header_name = f"x-picard-{token}"
            # Encode the header -- this inserts it into the dynamic table
            encoder.encode([(header_name, encoded_chunk)], huffman=False)
            return token

        token = await asyncio.to_thread(_write)
        self._reg[token] = encoded_chunk
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = json.loads(ref.location)["token"]
        if token not in self._reg:
            raise KeyError(f"hpack_table chunk not in registry: {token}")
        return self._reg[token]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = json.loads(ref.location)["token"]
        self._reg.pop(token, None)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        from picard.chunker import Chunk
        refs = []
        for token, data in self._reg.items():
            try:
                if Chunk.decode(data).manifest_id == manifest_id:
                    refs.append(StoredChunkRef(adapter_name=_NAME,
                                               location=json.dumps({"token": token})))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            import hpack
            enc = hpack.Encoder()
            enc.encode([("x-test", "test")], huffman=False)
            return True
        except ImportError:
            return False

    async def close(self) -> None:
        self._reg.clear()
