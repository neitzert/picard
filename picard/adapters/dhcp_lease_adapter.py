"""DHCP lease adapter -- stores chunks as fake dnsmasq lease file entries.

Each write_chunk appends N fake lease entries to a temp file. The encoded
chunk is split into 63-char segments (DNS label max), each stored as the
hostname field of a fake lease entry. Cold reads scan the file by token.

dnsmasq lease format: <epoch> <mac> <ip> <hostname> <client-id>
Stdlib only. No daemon required.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import tempfile
import time
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "dhcp_lease"
_SEGMENT_SIZE = 63  # DNS label max length
_LEASE_RE = re.compile(
    r"^(\d+)\s+([\da-f:]+)\s+([\d.]+)\s+(\S+)\s+(\S+)$", re.MULTILINE
)


def _mac_for_index(token_hex: str, idx: int) -> str:
    # Encode token prefix + index into a fake MAC
    tb = bytes.fromhex(token_hex[:8])
    return f"52:54:{tb[0]:02x}:{tb[1]:02x}:{(idx >> 8) & 0xFF:02x}:{idx & 0xFF:02x}"


def _ip_for_index(idx: int) -> str:
    return f"10.66.{(idx >> 8) & 0xFF}.{idx & 0xFF}"


def _encode_chunk(token: str, encoded_chunk: str) -> list[str]:
    """Return lease lines for the chunk."""
    raw = base64.urlsafe_b64encode(encoded_chunk.encode("utf-8")).decode("ascii")
    segments = [raw[i:i+_SEGMENT_SIZE] for i in range(0, len(raw), _SEGMENT_SIZE)]
    expiry = int(time.time()) + 86400
    lines = []
    for i, seg in enumerate(segments):
        mac = _mac_for_index(token, i)
        ip = _ip_for_index(i)
        hostname = f"picard-{token[:8]}-{i:04d}-{seg}"
        lines.append(f"{expiry} {mac} {ip} {hostname} *")
    return lines


def _decode_chunk(lines: list[str], token: str) -> str:
    """Reconstruct chunk from matching lease lines."""
    prefix = f"picard-{token[:8]}-"
    entries: list[tuple[int, str]] = []
    for line in lines:
        m = _LEASE_RE.match(line.strip())
        if not m:
            continue
        hostname = m.group(4)
        if not hostname.startswith(prefix):
            continue
        rest = hostname[len(prefix):]
        parts = rest.split("-", 1)
        if len(parts) != 2:
            continue
        try:
            idx = int(parts[0])
        except ValueError:
            continue
        entries.append((idx, parts[1]))
    entries.sort()
    raw = "".join(seg for _, seg in entries)
    return base64.urlsafe_b64decode(raw + "==").decode("utf-8")


class DhcpLeaseAdapter(Adapter):

    def __init__(self, *, lease_file: str | None = None) -> None:
        if lease_file is None:
            fd, lease_file = tempfile.mkstemp(prefix="picard_dhcp_", suffix=".leases")
            os.close(fd)
        self._lease_file = lease_file
        self._reg: dict[str, str] = {}  # token -> encoded_chunk

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            tags={"requires": "stdlib", "substrate": "dhcp-lease-file"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            token = uuid.uuid4().hex[:16]
            lines = _encode_chunk(token, encoded_chunk)
            with open(self._lease_file, "a") as f:
                for line in lines:
                    f.write(line + "\n")
            return token

        token = await asyncio.to_thread(_write)
        self._reg[token] = encoded_chunk
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token,
                               "lease_file": self._lease_file}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]

        def _read():
            with open(loc["lease_file"]) as f:
                lines = f.readlines()
            return _decode_chunk(lines, token)

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        token = loc["token"]
        self._reg.pop(token, None)
        prefix = f"picard-{token[:8]}-"

        def _del():
            try:
                with open(loc["lease_file"]) as f:
                    lines = f.readlines()
                keep = [l for l in lines if not re.search(rf"\s{re.escape(prefix)}", l)]
                with open(loc["lease_file"], "w") as f:
                    f.writelines(keep)
            except FileNotFoundError:
                pass

        await asyncio.to_thread(_del)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        from picard.chunker import Chunk
        refs = []
        for token, data in self._reg.items():
            try:
                if Chunk.decode(data).manifest_id == manifest_id:
                    refs.append(StoredChunkRef(adapter_name=_NAME,
                                               location=json.dumps({"token": token,
                                                "lease_file": self._lease_file})))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            return os.path.exists(self._lease_file) or True
        except Exception:
            return False

    async def close(self) -> None:
        pass
