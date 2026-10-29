"""Environment variable adapter -- stores chunks in the process environment.

Each write_chunk sets PICARD_<TOKEN>=<encoded_chunk> in os.environ.
Cold reads scan os.environ for matching keys. Data persists until the
process exits or the key is deleted.

Cross-platform. No root required.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "environ"


class EnvironAdapter(Adapter):

    def __init__(self) -> None:
        self._reg: dict[str, str] = {}  # token -> encoded_chunk

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=32768,
            persistent=False,
            ordered=False,
            tags={"requires": "stdlib", "substrate": "process-environment"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        key = f"PICARD_{token.upper()}"

        def _write():
            os.environ[key] = encoded_chunk

        await asyncio.to_thread(_write)
        self._reg[token] = encoded_chunk
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token, "key": key}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]
        key = loc["key"]
        value = os.environ.get(key)
        if value is None:
            raise KeyError(f"env var not found: {key}")
        return value

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        token = loc["token"]
        self._reg.pop(token, None)
        key = loc["key"]
        os.environ.pop(key, None)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        from picard.chunker import Chunk
        refs = []
        for token, data in self._reg.items():
            try:
                if Chunk.decode(data).manifest_id == manifest_id:
                    key = f"PICARD_{token.upper()}"
                    refs.append(StoredChunkRef(adapter_name=_NAME,
                                               location=json.dumps({"token": token,
                                                "key": key})))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            key = f"PICARD_HC_{uuid.uuid4().hex[:8].upper()}"
            os.environ[key] = "test"
            val = os.environ.get(key)
            del os.environ[key]
            return val == "test"
        except Exception:
            return False

    async def close(self) -> None:
        for token in list(self._reg):
            self._reg.pop(token, None)
