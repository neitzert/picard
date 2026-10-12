"""Environment variables adapter — stores chunks as env vars.

Each chunk is stored as an environment variable with a PICARD_
prefix. The variable name includes the chunk UUID.

In-process only — changes are visible only within the current
process (os.environ is process-local).
"""

from __future__ import annotations

import os
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_PREFIX = "PICARD_CHUNK_"


class EnvVarsAdapter(Adapter):
    """Scatter chunks into environment variables."""

    def __init__(
        self,
        *,
        max_chunk_bytes: int = 4096,
    ) -> None:
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="env_vars",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=1000,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_id = uuid.uuid4().hex[:12]
        var_name = f"{_PREFIX}{chunk_id}"
        os.environ[var_name] = encoded_chunk
        location = f"env://{var_name}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="env_vars", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        var_name = ref.location.replace("env://", "")
        value = os.environ.get(var_name)
        if value is not None:
            return value
        if ref.location in self._cache:
            return self._cache[ref.location]
        raise KeyError(f"chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        var_name = ref.location.replace("env://", "")
        self._cache.pop(ref.location, None)
        if var_name in os.environ:
            del os.environ[var_name]
            return True
        return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for key in os.environ:
            if key.startswith(_PREFIX):
                refs.append(
                    StoredChunkRef(
                        adapter_name="env_vars",
                        location=f"env://{key}",
                    )
                )
        return refs

    async def health_check(self) -> bool:
        return True

    async def capacity(self) -> dict[str, int | None]:
        count = 0
        used = 0
        for key, value in os.environ.items():
            if key.startswith(_PREFIX):
                count += 1
                used += len(value.encode())
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": count,
        }
