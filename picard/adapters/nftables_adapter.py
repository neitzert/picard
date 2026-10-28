"""nftables adapter -- stores chunks as IPv4 addresses in nftables named sets.

Each byte of a chunk maps to an IPv4 address 10.65.{byte_index}.{byte_value}
in a dedicated nftables set under a picard table. No file is written; data
lives in kernel netfilter state until the set is deleted.

Requires: Linux, CAP_NET_ADMIN, nftables (nft command).
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
    raise ImportError("nftables adapter requires Linux")

_ADAPTER_NAME = "nftables"


class NftablesAdapter(Adapter):
    """Store chunks as IPv4 address elements in nftables named sets."""

    def __init__(
        self,
        *,
        table: str = "picard",
        max_chunk_bytes: int = 200,
    ) -> None:
        self._table = table
        self._max = max_chunk_bytes
        self._registry: dict[str, tuple[str, str]] = {}  # set_name -> (encoded_chunk, manifest_id)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_ADAPTER_NAME,
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            tags={
                "substrate": "nftables-set",
                "requires": "Linux,root,nftables",
            },
        )

    async def _run_nft(self, script: str) -> tuple[int, str]:
        """Run an nft -f - script via stdin."""
        proc = await asyncio.create_subprocess_exec(
            "nft", "-f", "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(script.encode())
        return proc.returncode, stderr.decode().strip()

    async def _ensure_table(self) -> None:
        await self._run_nft(f"add table inet {self._table}\n")

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        data = encoded_chunk.encode()
        if len(data) > self._max:
            raise ValueError(f"Chunk too large: {len(data)} bytes > max {self._max}")
        try:
            manifest_id = Chunk.decode(encoded_chunk).manifest_id
        except Exception:
            manifest_id = ""

        await self._ensure_table()

        token = uuid.uuid4().hex[:16]
        set_name = f"picard_{token}"

        elements = ", ".join(f"10.65.{i}.{v}" for i, v in enumerate(data))
        script = (
            f"add table inet {self._table}\n"
            f"add set inet {self._table} {set_name} {{ type ipv4_addr; }}\n"
            f"add element inet {self._table} {set_name} {{ {elements} }}\n"
        )
        rc, err = await self._run_nft(script)
        if rc != 0:
            raise PermissionError(f"nft write failed: {err}")

        self._registry[set_name] = (encoded_chunk, manifest_id)
        return StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"nftables://{set_name}")

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        set_name = ref.location.removeprefix("nftables://")
        if set_name not in self._registry:
            raise KeyError(f"nftables chunk not found: {set_name}")
        return self._registry[set_name][0]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        set_name = ref.location.removeprefix("nftables://")
        if set_name not in self._registry:
            return False
        rc, _ = await self._run_nft(
            f"delete set inet {self._table} {set_name}\n"
        )
        del self._registry[set_name]
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"nftables://{sn}")
            for sn, (_, mid) in self._registry.items()
            if mid == manifest_id
        ]

    async def health_check(self) -> bool:
        return shutil.which("nft") is not None

    async def close(self) -> None:
        pass


