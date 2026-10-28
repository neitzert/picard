"""Network namespace route adapter -- stores chunks as blackhole routes in a private netns.

Each byte of a chunk maps to a blackhole route 10.66.{byte_index}.{byte_value}/32
in a dedicated network namespace. No file is written; data lives in the kernel
routing table of the namespace until flushed.

Requires: Linux, CAP_NET_ADMIN, iproute2 (ip command).
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

if sys.platform != "linux":
    raise ImportError("netns-route adapter requires Linux")

_ADAPTER_NAME = "netns-route"


class NetnsRouteAdapter(Adapter):
    """Store chunks as blackhole routes in a private Linux network namespace."""

    def __init__(
        self,
        *,
        netns: str = "picard-store",
        max_chunk_bytes: int = 200,
    ) -> None:
        self._netns = netns
        self._max = max_chunk_bytes
        self._registry: dict[str, tuple[int, str, str]] = {}  # token -> (n_bytes, encoded_chunk, manifest_id)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_ADAPTER_NAME,
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            tags={
                "substrate": "netns-routing-table",
                "requires": "Linux,root,iproute2",
            },
        )

    async def _run_ip(self, *args: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "ip", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return proc.returncode, stderr.decode().strip()

    async def _netns_cmd(self, *args: str) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            "ip", "netns", "exec", self._netns, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return proc.returncode, stdout.decode(), stderr.decode().strip()

    async def _ensure_netns(self) -> None:
        rc, _ = await self._run_ip("netns", "add", self._netns)
        # rc != 0 is fine if namespace already exists

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        data = encoded_chunk.encode()
        if len(data) > self._max:
            raise ValueError(f"Chunk too large: {len(data)} bytes > max {self._max}")
        try:
            manifest_id = Chunk.decode(encoded_chunk).manifest_id
        except Exception:
            manifest_id = ""

        await self._ensure_netns()

        # Flush existing routes to make space (one chunk at a time per netns)
        await self._netns_cmd("ip", "route", "flush", "table", "main")

        for i, v in enumerate(data):
            ip = f"10.66.{i}.{v}"
            rc, _, err = await self._netns_cmd("ip", "route", "replace", "blackhole", ip)
            if rc != 0:
                raise PermissionError(f"ip route replace failed: {err}")

        token = uuid.uuid4().hex[:16]
        self._registry[token] = (len(data), encoded_chunk, manifest_id)
        return StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"netns-route://{token}")

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = ref.location.removeprefix("netns-route://")
        if token not in self._registry:
            raise KeyError(f"netns-route chunk not found: {token}")
        return self._registry[token][1]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = ref.location.removeprefix("netns-route://")
        if token not in self._registry:
            return False
        await self._netns_cmd("ip", "route", "flush", "table", "main")
        del self._registry[token]
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"netns-route://{tok}")
            for tok, (_, __, mid) in self._registry.items()
            if mid == manifest_id
        ]

    async def health_check(self) -> bool:
        if shutil.which("ip") is None:
            return False
        rc, _ = await self._run_ip("netns", "list")
        return rc == 0

    async def close(self) -> None:
        pass
