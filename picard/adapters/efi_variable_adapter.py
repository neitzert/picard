"""EFI variable adapter -- stores chunks as UEFI runtime variables.

Each chunk is written to /sys/firmware/efi/efivars/ as a named variable
picard.<token>-<GUID>. Variables persist across reboots in firmware storage
until explicitly deleted. This is the most durable PICARD substrate.

WARNING: Requires root and real EFI firmware. Never writes to well-known EFI
variable names. Firmware variable storage is typically limited to 64 KB total;
OSError ENOSPC means firmware storage is full.

Requires: Linux, root, EFI firmware with efivars mounted.
"""

from __future__ import annotations

import asyncio
import glob
import os
import sys
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

if sys.platform != "linux":
    raise ImportError("efi-variable adapter requires Linux")

_ADAPTER_NAME = "efi-variable"
_EFIVARS_PATH = "/sys/firmware/efi/efivars"
# Custom GUID: "picard" encoded to avoid collisions with any real EFI variable
_PICARD_GUID = "61636172-6463-0000-0000-706963617264"
# EFI variable attributes: NV (0x1) | BS (0x2) | RT (0x4) = 0x7
_EFI_ATTRS = b"\x07\x00\x00\x00"


class EfiVariableAdapter(Adapter):
    """Store chunks as UEFI runtime variables in efivars filesystem."""

    def __init__(
        self,
        *,
        max_chunk_bytes: int = 8192,
        guid: str = _PICARD_GUID,
    ) -> None:
        self._max = max_chunk_bytes
        self._guid = guid
        self._registry: dict[str, tuple[str, str]] = {}  # var_name -> (encoded_chunk, manifest_id)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_ADAPTER_NAME,
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            tags={
                "substrate": "uefi-efi-variables",
                "requires": "Linux,root,EFI-firmware",
                "WARNING": "firmware-persistent-survives-reboot",
            },
        )

    def _var_path(self, var_name: str) -> str:
        return os.path.join(_EFIVARS_PATH, var_name)

    async def _chattr_i(self, path: str) -> None:
        """Remove immutable flag from an efivar file."""
        proc = await asyncio.create_subprocess_exec(
            "chattr", "-i", path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise PermissionError(
                f"chattr -i failed on {path}: {stderr.decode().strip()}. "
                "Root and chattr (e2fsprogs) are required."
            )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        data = encoded_chunk.encode()
        if len(data) > self._max:
            raise ValueError(f"Chunk too large: {len(data)} bytes > max {self._max}")
        if not os.path.isdir(_EFIVARS_PATH):
            raise RuntimeError(f"EFI variables not available: {_EFIVARS_PATH} not found")
        try:
            manifest_id = Chunk.decode(encoded_chunk).manifest_id
        except Exception:
            manifest_id = ""

        token = uuid.uuid4().hex[:16]
        var_name = f"picard.{token}-{self._guid}"

        # Safety: only ever write to picard.* names
        if not var_name.startswith("picard."):
            raise ValueError(f"Refusing to write non-picard variable: {var_name}")

        path = self._var_path(var_name)
        content = _EFI_ATTRS + data

        if os.path.exists(path):
            await self._chattr_i(path)

        try:
            with open(path, "wb") as f:
                f.write(content)
        except OSError as e:
            if e.errno == 28:  # ENOSPC
                raise RuntimeError(
                    "EFI variable storage full (ENOSPC). "
                    "Delete unused EFI variables to free space."
                ) from e
            raise

        self._registry[var_name] = (encoded_chunk, manifest_id)
        return StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"efi-variable://{var_name}")

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        var_name = ref.location.removeprefix("efi-variable://")
        if var_name not in self._registry:
            raise KeyError(f"efi-variable chunk not found: {var_name}")
        # Try filesystem first for persistence across restarts
        path = self._var_path(var_name)
        if os.path.exists(path):
            with open(path, "rb") as f:
                raw = f.read()
            return raw[4:].decode()  # skip 4-byte attributes header
        return self._registry[var_name][0]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        var_name = ref.location.removeprefix("efi-variable://")
        if var_name not in self._registry:
            return False
        path = self._var_path(var_name)
        if os.path.exists(path):
            try:
                await self._chattr_i(path)
                os.unlink(path)
            except Exception:
                pass
        del self._registry[var_name]
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"efi-variable://{vn}")
            for vn, (_, mid) in self._registry.items()
            if mid == manifest_id
        ]

    async def health_check(self) -> bool:
        return os.path.isdir(_EFIVARS_PATH) and os.access(_EFIVARS_PATH, os.R_OK)

    async def close(self) -> None:
        pass
