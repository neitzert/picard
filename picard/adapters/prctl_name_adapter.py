"""prctl thread name adapter -- stores chunk tokens in Linux thread names.

Each write_chunk spawns a daemon thread named 'picard.<token[:13]>' via
prctl(PR_SET_NAME). Thread names are visible in /proc/<pid>/task/<tid>/comm
and in 'ps -eLo pid,tid,comm'. The encoded chunk is held in-process;
the thread name is the durable kernel commitment.

Linux-only. No root required.
"""
from __future__ import annotations

import sys
if sys.platform != "linux":
    raise ImportError("prctl_name adapter requires Linux")

import asyncio
import ctypes
import json
import threading
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "prctl_name"
_PR_SET_NAME = 15

def _libc():
    return ctypes.CDLL("libc.so.6", use_errno=True)


class PrctlNameAdapter(Adapter):

    def __init__(self) -> None:
        self._reg: dict[str, tuple[threading.Event, str]] = {}  # token -> (stop_event, chunk)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=False,
            ordered=False,
            tags={"requires": "Linux", "substrate": "thread-name-prctl"},
        )

    def _spawn_thread(self, token: str) -> threading.Event:
        stop = threading.Event()
        name = f"picard.{token[:13]}"

        def _run():
            _libc().prctl(_PR_SET_NAME, name.encode()[:15], 0, 0, 0)
            stop.wait()

        t = threading.Thread(target=_run, daemon=True, name=name)
        t.start()
        return stop

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        stop = await asyncio.to_thread(self._spawn_thread, token)
        self._reg[token] = (stop, encoded_chunk)
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = json.loads(ref.location)["token"]
        if token not in self._reg:
            raise KeyError(f"prctl_name chunk not in registry: {token}")
        return self._reg[token][1]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = json.loads(ref.location)["token"]
        entry = self._reg.pop(token, None)
        if entry:
            entry[0].set()  # signal thread to exit
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
            libc = _libc()
            result = libc.prctl(_PR_SET_NAME, b"picard.hc", 0, 0, 0)
            return result == 0
        except Exception:
            return False

    async def close(self) -> None:
        for token in list(self._reg):
            entry = self._reg.pop(token, None)
            if entry:
                entry[0].set()
