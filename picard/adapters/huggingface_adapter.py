"""Hugging Face Hub adapter — stores chunks as dataset files.

Chunks are uploaded as files to a Hugging Face dataset repository.

Requires: pip install picard[huggingface]  (aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

import aiohttp

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_HF_API = "https://huggingface.co/api"


class HuggingFaceAdapter(Adapter):
    """Scatter chunks into Hugging Face Hub repositories."""

    def __init__(
        self,
        token: str,
        *,
        repo_id: str = "picard/chunks",
        repo_type: str = "dataset",
        max_chunk_bytes: int = 10485760,
    ) -> None:
        self._token = token
        self._repo_id = repo_id
        self._repo_type = repo_type
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}
        self._session = None


    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="huggingface",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            filename = f"{chunk.manifest_id}/{chunk.block_index}_{chunk.chunk_index}.pchk"
        except ValueError:
            filename = f"chunks/{uuid.uuid4().hex}.pchk"

        upload_url = (
            f"{_HF_API}/{self._repo_type}s/{self._repo_id}/upload/main/{filename}"
        )

        session = await self._get_session()
        resp = await session.put(
            upload_url,
            headers=self._headers(),
            data=encoded_chunk.encode(),
        )
        resp.raise_for_status()

        loc = f"hf://{self._repo_id}/{filename}"
        self._cache[loc] = encoded_chunk
        return StoredChunkRef(adapter_name="huggingface", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]

        path = ref.location.replace(f"hf://{self._repo_id}/", "")
        resolve_url = (
            f"https://huggingface.co/{self._repo_type}s/{self._repo_id}/resolve/main/{path}"
        )

        session = await self._get_session()
        resp = await session.get(resolve_url, headers=self._headers())
        resp.raise_for_status()
        return await resp.text()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        self._cache.pop(ref.location, None)
        path = ref.location.replace(f"hf://{self._repo_id}/", "")

        try:
            session = await self._get_session()
            resp = await session.delete(
                f"{_HF_API}/{self._repo_type}s/{self._repo_id}/delete/main/{path}",
                headers=self._headers(),
            )
            resp.raise_for_status()
            return True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="huggingface", location=loc)
            for loc in self._cache
            if manifest_id in loc
        ]

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            resp = await session.get(
                f"{_HF_API}/whoami",
                headers=self._headers(),
            )
            return resp.status == 200
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        return {
            "used_bytes": sum(len(v.encode()) for v in self._cache.values()),
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._cache),
        }
