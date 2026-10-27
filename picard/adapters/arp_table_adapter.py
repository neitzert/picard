"""ARP table adapter -- stores chunks in kernel neighbor table entries.

Each byte of a chunk maps to a permanent ARP entry on a dedicated dummy
interface. Byte index i with value v becomes IP 10.61.i.1 with MAC
02:61:ii:vv:00:00. No file is written; data lives in the kernel
neighbor cache until explicitly flushed or the interface is removed.

Requires: Linux, CAP_NET_ADMIN, iproute2 (ip command).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_ADAPTER_NAME = "arp-table"


def _require_linux() -> None:
    if sys.platform != "linux":
        raise NotImplementedError(f"{_ADAPTER_NAME} requires Linux")


class ArpTableAdapter(Adapter):
    """Scatter chunks into kernel ARP/neighbor table entries."""

    def __init__(
        self,
        *,
        iface: str = "picard-arp0",
        max_chunk_bytes: int = 64,
    ) -> None:
        _require_linux()
        self._iface = iface
        self._max = max_chunk_bytes
        self._registry: dict[str, str] = {}  # location json -> manifest_id
        self._created = self._ensure_iface()

    def _ensure_iface(self) -> bool:
        result = subprocess.run(
            ["ip", "link", "show", self._iface],
            capture_output=True,
        )
        if result.returncode == 0:
            return False
        add = subprocess.run(
            ["ip", "link", "add", self._iface, "type", "dummy"],
            capture_output=True,
        )
        if add.returncode != 0:
            raise PermissionError(
                f"Cannot create dummy interface {self._iface!r}: "
                f"{add.stderr.decode().strip()}. Requires CAP_NET_ADMIN."
            )
        subprocess.run(["ip", "link", "set", self._iface, "up"], check=True)
        return True

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_ADAPTER_NAME,
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=120,
            tags={
                "substrate": "kernel-arp-table",
                "requires": "Linux,CAP_NET_ADMIN,iproute2",
            },
        )

    def _ip_for(self, i: int) -> str:
        return f"10.61.{i}.1"

    def _mac_for(self, i: int, v: int) -> str:
        return f"02:61:{i:02x}:{v:02x}:00:00"

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        data = encoded_chunk.encode()
        if len(data) > self._max:
            raise ValueError(
                f"Chunk too large: {len(data)} bytes > max {self._max}"
            )
        try:
            manifest_id = Chunk.decode(encoded_chunk).manifest_id
        except Exception:
            manifest_id = ""

        # Flush existing entries first so a second write is idempotent.
        subprocess.run(
            ["ip", "neigh", "flush", "dev", self._iface],
            capture_output=True,
        )

        for i, v in enumerate(data):
            ip = self._ip_for(i)
            mac = self._mac_for(i, v)
            result = subprocess.run(
                ["ip", "neigh", "add", ip, "lladdr", mac,
                 "dev", self._iface, "nud", "permanent"],
                capture_output=True,
            )
            if result.returncode != 0:
                raise PermissionError(
                    f"ip neigh add failed: {result.stderr.decode().strip()}"
                )

        loc = json.dumps({
            "iface": self._iface,
            "n_bytes": len(data),
            "manifest_id": manifest_id,
        }, separators=(",", ":"))
        self._registry[loc] = manifest_id
        return StoredChunkRef(adapter_name=_ADAPTER_NAME, location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location not in self._registry:
            raise KeyError(f"arp-table chunk not found: {ref.location[:60]}")

        loc = json.loads(ref.location)
        n_bytes: int = loc["n_bytes"]
        iface: str = loc.get("iface", self._iface)

        result = subprocess.run(
            ["ip", "neigh", "show", "dev", iface],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"ip neigh show failed: {result.stderr.strip()}")

        byte_map: dict[int, int] = {}
        for line in result.stdout.splitlines():
            # "10.61.3.1 lladdr 02:61:03:41:00:00 PERMANENT"
            parts = line.split()
            if len(parts) < 3:
                continue
            ip = parts[0]
            if not ip.startswith("10.61."):
                continue
            ip_parts = ip.split(".")
            if len(ip_parts) != 4:
                continue
            try:
                idx = int(ip_parts[2])
            except ValueError:
                continue
            lladdr_idx = parts.index("lladdr") if "lladdr" in parts else -1
            if lladdr_idx < 0:
                continue
            mac = parts[lladdr_idx + 1]
            mac_parts = mac.split(":")
            if len(mac_parts) != 6:
                continue
            try:
                val = int(mac_parts[3], 16)
            except ValueError:
                continue
            byte_map[idx] = val

        if len(byte_map) < n_bytes:
            raise RuntimeError(
                f"Expected {n_bytes} ARP entries, found {len(byte_map)}"
            )

        raw = bytes(byte_map[i] for i in range(n_bytes))
        return raw.decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        if ref.location not in self._registry:
            return False
        loc = json.loads(ref.location)
        iface = loc.get("iface", self._iface)
        subprocess.run(
            ["ip", "neigh", "flush", "dev", iface],
            capture_output=True,
        )
        del self._registry[ref.location]
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name=_ADAPTER_NAME, location=loc)
            for loc, mid in self._registry.items()
            if mid == manifest_id
        ]

    async def health_check(self) -> bool:
        result = subprocess.run(
            ["ip", "link", "show", self._iface],
            capture_output=True,
        )
        return result.returncode == 0

    async def close(self) -> None:
        if self._created:
            subprocess.run(
                ["ip", "link", "del", self._iface],
                capture_output=True,
            )
