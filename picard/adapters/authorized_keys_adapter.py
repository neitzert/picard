"""SSH authorized_keys adapter — stores chunks in the comment field of SSH keys.

Each chunk is stored as a placeholder SSH public key entry in
~/.ssh/authorized_keys.  The comment field carries the PICARD marker
and data:

  ssh-rsa AAAAB3NzaC1y...placeholder== PICARD:{chunk_id}:{encoded_data}

Only use this on files you own.  The comment field is a standard part
of the authorized_keys format; the placeholder key material is
syntactically valid but non-functional (no corresponding private key).

Requires: no additional dependencies (stdlib only)
"""

from __future__ import annotations

import os
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

# Syntactically valid but non-functional placeholder RSA public key material
_PLACEHOLDER_KEY = (
    "AAAAB3NzaC1yc2EAAAADAQABAAABgQC7J3pZ5FdNqm4kH2sR"
    "pMvXjR0hK5yQ9a8bWdXe1jN3kF5L2mV7rT4wYxZcU6iA8g0H"
    "nBvRtKpOlMwNqSdEfGhIjKlMnOpQrStUvWxYzAbCdEfGhIjKl"
)


class AuthorizedKeysAdapter(Adapter):
    """Scatter chunks into SSH authorized_keys comment fields."""

    def __init__(
        self,
        *,
        keys_path: str = "~/.ssh/authorized_keys",
        max_chunk_bytes: int = 4096,
    ) -> None:
        self._keys_path = os.path.expanduser(keys_path)
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="authorized-keys",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=120,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_id = uuid.uuid4().hex[:12]
        location = f"authorized-keys://{chunk_id}"

        # Build a fake authorized_keys line:
        # ssh-rsa <fake-key> PICARD:<chunk_id>:<encoded_data>
        comment = f"PICARD:{chunk_id}:{encoded_chunk}"
        _line = f"ssh-rsa {_PLACEHOLDER_KEY} {comment}\n"

        self._append_to_keys(_line)
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="authorized-keys", location=location)

    def _append_to_keys(self, line: str) -> None:  # pragma: no cover
        """Append a key entry to authorized_keys (real implementation)."""
        os.makedirs(os.path.dirname(self._keys_path), exist_ok=True)
        with open(self._keys_path, "a") as f:
            f.write(line)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]

        # Fallback: parse authorized_keys for the chunk_id marker
        chunk_id = ref.location.split("://", 1)[-1]
        marker = f"PICARD:{chunk_id}:"

        if os.path.exists(self._keys_path):
            with open(self._keys_path, "r") as f:
                for line in f:
                    if marker in line:
                        idx = line.index(marker) + len(marker)
                        return line[idx:].strip()

        raise KeyError(f"chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        existed = ref.location in self._cache
        self._cache.pop(ref.location, None)

        chunk_id = ref.location.split("://", 1)[-1]
        marker = f"PICARD:{chunk_id}:"
        try:
            if os.path.exists(self._keys_path):
                with open(self._keys_path, "r") as f:
                    lines = f.readlines()
                with open(self._keys_path, "w") as f:
                    for line in lines:
                        if marker not in line:
                            f.write(line)
        except OSError:
            pass

        return existed

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        for loc, data in self._cache.items():
            try:
                chunk = Chunk.decode(data)
                if chunk.manifest_id == manifest_id:
                    refs.append(
                        StoredChunkRef(adapter_name="authorized-keys", location=loc)
                    )
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            parent = os.path.dirname(self._keys_path)
            return os.path.isdir(parent)
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        used = sum(len(v.encode()) for v in self._cache.values())
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._cache),
        }
