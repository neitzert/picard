"""DHCP lease table adapter.

Encodes chunks as fake lease entries in a dnsmasq-format lease file.
Each entry uses the hostname field to carry base64url-encoded chunk data
across N sequential records. No daemon required -- pure file manipulation.

Lease file format (dnsmasq): <expiry> <mac> <ip> <hostname> <client-id>

Requires: writable lease file path. dnsmasq optional (for health_check).
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import sys
import time
import uuid
from typing import Sequence

try:
    import fcntl as _fcntl
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_ADAPTER_NAME = "dhcp-lease"
_SEG_LEN = 50  # characters of base64url data per hostname entry


class DhcpLeaseAdapter(Adapter):
    """Store chunks as fake dnsmasq DHCP lease entries."""

    def __init__(
        self,
        lease_file: str = "/var/lib/misc/dnsmasq.leases",
        *,
        max_chunk_bytes: int = 65536,
    ) -> None:
        self._lease_file = lease_file
        self._max = max_chunk_bytes
        self._registry: dict[str, tuple[str, str]] = {}  # token -> (encoded_chunk, manifest_id)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_ADAPTER_NAME,
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            tags={
                "substrate": "dhcp-lease-table",
                "requires": "dnsmasq(optional),writable-lease-file",
            },
        )

    def _expiry(self) -> int:
        return int(time.time()) + 86400 * 365 * 10

    def _mac(self, token: str, seq: int) -> str:
        t0 = int(token[:2], 16)
        t1 = int(token[2:4], 16)
        return f"52:54:00:{t0:02x}:{t1:02x}:{seq & 0xFF:02x}"

    def _ip(self, token: str, seq: int) -> str:
        nibble = int(token[4:6], 16) & 0xFF
        return f"198.18.{nibble}.{seq & 0xFF}"

    def _hostname(self, token: str, seq: int, segment: str) -> str:
        return f"picard-{token}-{seq:04d}-{segment}"

    def _flock(self, f: object, mode: int) -> None:
        if _HAS_FCNTL:
            _fcntl.flock(f, mode)  # type: ignore[arg-type]

    def _write_lines(self, lines: list[str]) -> None:
        parent = os.path.dirname(self._lease_file)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(self._lease_file, "a") as f:
            self._flock(f, 2)  # LOCK_EX = 2
            try:
                for line in lines:
                    f.write(line + "\n")
            finally:
                self._flock(f, 8)  # LOCK_UN = 8

    def _read_lines(self) -> list[str]:
        try:
            with open(self._lease_file) as f:
                self._flock(f, 1)  # LOCK_SH = 1
                try:
                    return f.readlines()
                finally:
                    self._flock(f, 8)  # LOCK_UN = 8
        except FileNotFoundError:
            return []

    def _rewrite_without(self, token: str) -> None:
        try:
            lines = self._read_lines()
            pattern = f"picard-{token}-"
            kept = [line for line in lines if pattern not in line]
            with open(self._lease_file, "w") as f:
                self._flock(f, 2)  # LOCK_EX = 2
                try:
                    f.writelines(kept)
                finally:
                    self._flock(f, 8)  # LOCK_UN = 8
        except FileNotFoundError:
            pass

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        data = encoded_chunk.encode()
        if len(data) > self._max:
            raise ValueError(f"Chunk too large: {len(data)} > {self._max}")
        try:
            manifest_id = Chunk.decode(encoded_chunk).manifest_id
        except Exception:
            manifest_id = ""

        token = uuid.uuid4().hex[:16]
        b64 = base64.urlsafe_b64encode(data).decode()
        segments = [b64[i : i + _SEG_LEN] for i in range(0, len(b64), _SEG_LEN)]

        expiry = self._expiry()
        lines = []
        for seq, seg in enumerate(segments):
            mac = self._mac(token, seq)
            ip = self._ip(token, seq)
            hostname = self._hostname(token, seq, seg)
            lines.append(f"{expiry} {mac} {ip} {hostname} *")

        self._write_lines(lines)
        self._registry[token] = (encoded_chunk, manifest_id)
        return StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"dhcp-lease://{token}")

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = ref.location.removeprefix("dhcp-lease://")
        # In-process registry first
        if token in self._registry:
            return self._registry[token][0]
        # Reconstruct from lease file
        lines = self._read_lines()
        pattern = re.compile(rf"picard-{re.escape(token)}-(\d{{4}})-(\S+)")
        entries: dict[int, str] = {}
        for line in lines:
            m = pattern.search(line)
            if m:
                seq = int(m.group(1))
                seg = m.group(2).split()[0]  # hostname is 5th field; already parsed
                entries[seq] = seg
        if not entries:
            raise KeyError(f"dhcp-lease chunk not found: {token}")
        b64 = "".join(entries[i] for i in sorted(entries))
        # Restore padding
        pad = (4 - len(b64) % 4) % 4
        b64 += "=" * pad
        return base64.urlsafe_b64decode(b64).decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = ref.location.removeprefix("dhcp-lease://")
        existed = token in self._registry
        self._registry.pop(token, None)
        self._rewrite_without(token)
        return existed

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        for tok, (chunk, mid) in self._registry.items():
            if mid == manifest_id:
                refs.append(StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"dhcp-lease://{tok}"))
        # Also scan lease file for tokens not in local registry
        lines = self._read_lines()
        seen = {r.location.removeprefix("dhcp-lease://") for r in refs}
        pattern = re.compile(r"picard-([0-9a-f]{16})-0000-")
        for line in lines:
            m = pattern.search(line)
            if m:
                tok = m.group(1)
                if tok not in seen:
                    # Can't determine manifest_id without reading the chunk; skip
                    pass
        return refs

    async def health_check(self) -> bool:
        return shutil.which("dnsmasq") is not None or os.path.exists(self._lease_file)

    async def close(self) -> None:
        pass
