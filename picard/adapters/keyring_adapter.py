"""Kernel keyring adapter -- stores chunks as Linux kernel keyring entries.

Each chunk is a ``user`` key in the kernel keyring. Keys live in kernel
memory, not on disk or in any filesystem. They are invisible to ``ps``,
``/proc``, and ``ls``. The session keyring (``@s``) is the default target;
use ``@u`` (user keyring) for persistence across login sessions.

The 32 KB per-key limit is a hard kernel constraint for ``user`` type keys.
Keys evict automatically when the session or user keyring is destroyed.

Requires: Linux with ``keyctl`` on PATH. No elevated privileges needed for
the session or user keyring.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

if sys.platform != "linux":
    raise NotImplementedError("keyring_adapter requires Linux")


class KeyringAdapter(Adapter):
    """Scatter chunks into the Linux kernel keyring."""

    def __init__(
        self,
        *,
        keyring: str = "@s",
        max_chunk_bytes: int = 32767,
    ) -> None:
        self._keyring = keyring
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="kernel-keyring",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=500,
            tags={
                "substrate": "kernel-keyring",
                "requires": "Linux,keyctl",
            },
        )

    async def _keyctl(self, *args: str, stdin: bytes | None = None) -> str:
        proc = await asyncio.create_subprocess_exec(
            "keyctl", *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(input=stdin)
        if proc.returncode != 0:
            raise RuntimeError(
                f"keyctl {' '.join(args)} failed: {stderr.decode().strip()}"
            )
        return stdout.decode().strip()

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import uuid
        token = uuid.uuid4().hex[:16]
        description = f"picard.{token}"
        key_id = await self._keyctl(
            "padd", "user", description, self._keyring,
            stdin=encoded_chunk.encode(),
        )
        location = f"keyring://{key_id}/{token}"
        return StoredChunkRef(adapter_name="kernel-keyring", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        key_id = ref.location.split("/")[2]
        try:
            return await self._keyctl("pipe", key_id)
        except RuntimeError as exc:
            raise KeyError(f"chunk not found: {ref.location}") from exc

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        key_id = ref.location.split("/")[2]
        try:
            await self._keyctl("revoke", key_id)
            return True
        except RuntimeError:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        try:
            output = await self._keyctl("list", self._keyring)
        except RuntimeError:
            return refs

        for line in output.splitlines():
            line = line.strip()
            if not line or "picard." not in line:
                continue
            # Format: "<key_id>: --alswrv ... user: picard.<token>"
            parts = line.split(":")
            if len(parts) < 2:
                continue
            key_id = parts[0].strip()
            # Extract token from the description field
            desc_part = ":".join(parts[1:])
            if "picard." not in desc_part:
                continue
            token_part = desc_part.split("picard.", 1)[-1].strip()
            token = token_part.split()[0] if token_part else ""
            if not token or not key_id.isdigit():
                continue
            try:
                data = await self._keyctl("pipe", key_id)
                if Chunk.decode(data).manifest_id == manifest_id:
                    location = f"keyring://{key_id}/{token}"
                    refs.append(StoredChunkRef(adapter_name="kernel-keyring", location=location))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            await self._keyctl("show")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        pass

    async def capacity(self) -> dict[str, int | None]:
        return {
            "used_bytes": None,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": None,
        }
