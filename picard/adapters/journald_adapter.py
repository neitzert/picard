"""Journald adapter -- stores chunks as structured systemd journal entries.

Each chunk is written as a structured journal entry with custom fields:
PICARD_TOKEN, PICARD_MANIFEST, PICARD_CHUNK. Entries persist until journal
rotation. Individual deletion is not supported by journald -- delete_chunk
marks the token deleted in the in-process registry and logs a tombstone.

Primary: sd_journal_send via ctypes (libsystemd.so.0).
Fallback: systemd-cat subprocess.

Requires: Linux with systemd. No elevated privileges needed.
"""

from __future__ import annotations

import asyncio
import ctypes
import shutil
import sys
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

if sys.platform != "linux":
    raise ImportError("journald_adapter requires Linux")

_ADAPTER_NAME = "journald"

try:
    _libsystemd: ctypes.CDLL | None = ctypes.CDLL("libsystemd.so.0", use_errno=True)
    _libsystemd.sd_journal_send.restype = ctypes.c_int
except OSError:
    _libsystemd = None


class JournaldAdapter(Adapter):
    """Scatter chunks into the systemd journal as structured log entries."""

    def __init__(self, *, max_chunk_bytes: int = 65536) -> None:
        self._max = max_chunk_bytes
        self._registry: dict[str, tuple[str, str]] = {}  # token -> (encoded_chunk, manifest_id)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_ADAPTER_NAME,
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=500,
            tags={
                "substrate": "systemd-journal",
                "requires": "Linux,systemd",
                "note": "delete marks in-process only; journal entry persists until rotation",
            },
        )

    def _journal_send(self, token: str, manifest_id: str, encoded_chunk: str) -> None:
        if _libsystemd is not None:
            _libsystemd.sd_journal_send(
                b"MESSAGE=picard chunk",
                b"PICARD_TOKEN=" + token.encode(),
                b"PICARD_MANIFEST=" + manifest_id.encode(),
                b"PICARD_CHUNK=" + encoded_chunk.encode(),
                None,
            )
        # fallback via systemd-cat happens async in write_chunk

    async def _systemd_cat_send(self, token: str, manifest_id: str, encoded_chunk: str) -> None:
        payload = (
            f"PICARD_TOKEN={token}\n"
            f"PICARD_MANIFEST={manifest_id}\n"
            f"PICARD_CHUNK={encoded_chunk}\n"
        ).encode()
        proc = await asyncio.create_subprocess_exec(
            "systemd-cat", "-t", "picard",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate(input=payload)

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        if len(encoded_chunk.encode()) > self._max:
            raise ValueError(
                f"Chunk too large: {len(encoded_chunk.encode())} > {self._max}"
            )
        try:
            manifest_id = Chunk.decode(encoded_chunk).manifest_id
        except Exception:
            manifest_id = ""

        token = uuid.uuid4().hex[:16]
        if _libsystemd is not None:
            self._journal_send(token, manifest_id, encoded_chunk)
        else:
            await self._systemd_cat_send(token, manifest_id, encoded_chunk)

        self._registry[token] = (encoded_chunk, manifest_id)
        return StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"journald://{token}")

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = ref.location.removeprefix("journald://")
        if token in self._registry:
            return self._registry[token][0]
        # Fallback: query journalctl by the exact PICARD_TOKEN field value.
        # -t picard would filter by SYSLOG_IDENTIFIER which we don't set.
        try:
            proc = await asyncio.create_subprocess_exec(
                "journalctl", "--output=json", "--no-pager",
                f"PICARD_TOKEN={token}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            import json
            for line in stdout.decode().splitlines():
                try:
                    entry = json.loads(line)
                    if entry.get("PICARD_TOKEN") == token:
                        return entry["PICARD_CHUNK"]
                except Exception:
                    continue
        except Exception:
            pass
        raise KeyError(f"chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = ref.location.removeprefix("journald://")
        existed = token in self._registry
        if existed:
            _, manifest_id = self._registry.pop(token)
            # Log tombstone so listeners know it's gone
            if _libsystemd is not None:
                _libsystemd.sd_journal_send(
                    b"MESSAGE=picard chunk deleted",
                    b"PICARD_DELETED=" + token.encode(),
                    None,
                )
        return existed

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"journald://{token}")
            for token, (_, mid) in self._registry.items()
            if mid == manifest_id
        ]

    async def health_check(self) -> bool:
        if _libsystemd is not None:
            return True
        return shutil.which("systemd-cat") is not None

    async def close(self) -> None:
        pass
