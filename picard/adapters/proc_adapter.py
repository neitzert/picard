"""procfs adapter — stores chunks in process environment variables.

Data lives in the process's environment, readable via /proc/self/environ
on Linux.  Dies when the process exits (ephemeral storage only).
"""

from __future__ import annotations

import os
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_VAR_PREFIX = "PICARD_CHUNK_"


class ProcAdapter(Adapter):
    """Scatter chunks into process environment variables."""

    def __init__(
        self,
        *,
        pid: str = "self",
        max_chunk_bytes: int = 4096,
    ) -> None:
        self._pid = pid
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="procfs",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=60,
        )

    def _chunk_key(self, encoded_chunk: str) -> str:
        from picard.chunker import Chunk

        try:
            chunk = Chunk.decode(encoded_chunk)
            return f"{chunk.manifest_id}_{chunk.block_index}_{chunk.chunk_index}"
        except (ValueError, Exception):
            return uuid.uuid4().hex

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        key = self._chunk_key(encoded_chunk)
        env_key = f"{_VAR_PREFIX}{key}"
        os.environ[env_key] = encoded_chunk
        location = f"proc://{self._pid}/environ/{key}"
        return StoredChunkRef(adapter_name="procfs", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        key = ref.location.rsplit("/", 1)[-1]
        env_key = f"{_VAR_PREFIX}{key}"
        value = os.environ.get(env_key)
        if value is None:
            raise KeyError(f"env var not found: {env_key}")
        return value

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        key = ref.location.rsplit("/", 1)[-1]
        env_key = f"{_VAR_PREFIX}{key}"
        try:
            del os.environ[env_key]
            return True
        except KeyError:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        prefix = f"{_VAR_PREFIX}{manifest_id}"
        refs: list[StoredChunkRef] = []
        for env_key in os.environ:
            if env_key.startswith(prefix):
                key = env_key[len(_VAR_PREFIX):]
                loc = f"proc://{self._pid}/environ/{key}"
                refs.append(StoredChunkRef(adapter_name="procfs", location=loc))
        return refs

    async def health_check(self) -> bool:
        test_key = f"{_VAR_PREFIX}__health__"
        try:
            os.environ[test_key] = "1"
            del os.environ[test_key]
            return True
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        count = sum(1 for k in os.environ if k.startswith(_VAR_PREFIX))
        used = sum(
            len(v.encode())
            for k, v in os.environ.items()
            if k.startswith(_VAR_PREFIX)
        )
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": count,
        }
