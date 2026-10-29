"""CPU affinity adapter -- stores chunk tokens in thread CPU affinity masks.

Each write_chunk spawns a daemon thread and sets its CPU affinity to a
bitmask derived from the chunk's hash. The affinity is visible via
os.sched_getaffinity(tid) or in /proc/<pid>/task/<tid>/status.
Chunk data is held in-process registry.

Linux-only. No root required.
"""
from __future__ import annotations

import sys
if sys.platform != "linux":
    raise ImportError("cpu_affinity adapter requires Linux")

import asyncio
import hashlib
import json
import os
import threading
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "cpu_affinity"


class CpuAffinityAdapter(Adapter):

    def __init__(self) -> None:
        self._reg: dict[str, tuple[threading.Event, str]] = {}
        self._cpu_count = os.cpu_count() or 1

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=False,
            ordered=False,
            tags={"requires": "Linux", "substrate": "cpu-affinity-bitmask"},
        )

    def _affinity_from_chunk(self, encoded_chunk: str) -> set:
        h = int.from_bytes(hashlib.sha256(encoded_chunk.encode()).digest()[:4], "big")
        cpus = set()
        for i in range(self._cpu_count):
            if h & (1 << (i % 32)):
                cpus.add(i)
        return cpus or {0}

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        affinity = self._affinity_from_chunk(encoded_chunk)
        stop = threading.Event()

        def _run():
            try:
                os.sched_setaffinity(0, affinity)
            except Exception:
                pass
            stop.wait()

        t = threading.Thread(target=_run, daemon=True, name=f"picard.aff.{token[:8]}")
        t.start()
        self._reg[token] = (stop, encoded_chunk)
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = json.loads(ref.location)["token"]
        if token not in self._reg:
            raise KeyError(f"cpu_affinity chunk not in registry: {token}")
        return self._reg[token][1]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = json.loads(ref.location)["token"]
        entry = self._reg.pop(token, None)
        if entry:
            entry[0].set()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        from picard.chunker import Chunk
        refs = []
        for token, (_, data) in self._reg.items():
            try:
                if Chunk.decode(data).manifest_id == manifest_id:
                    refs.append(StoredChunkRef(adapter_name=_NAME,
                                               location=json.dumps({"token": token})))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            affinity = os.sched_getaffinity(0)
            return len(affinity) > 0
        except Exception:
            return False

    async def close(self) -> None:
        for token in list(self._reg):
            entry = self._reg.pop(token, None)
            if entry:
                entry[0].set()
