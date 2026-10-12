"""GitLab Snippet adapter — stores chunks as private snippets.

Each chunk becomes a snippet:
    glsnip://{snippet_id}

Requires: pip install picard-store[gitlab]
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class GitLabSnippetAdapter(Adapter):
    """Scatter chunks into private GitLab Snippets."""

    def __init__(
        self,
        private_token: str,
        *,
        gitlab_url: str = "https://gitlab.com",
        max_chunk_bytes: int = 10_485_760,
    ) -> None:
        self._token = private_token
        self._url = gitlab_url.rstrip("/")
        self._max = max_chunk_bytes
        self._session = None


    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _get_session(self):
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="gitlab-snippet",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    def _headers(self) -> dict:
        return {"PRIVATE-TOKEN": self._token}

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            filename = f"{chunk.manifest_id}_{chunk.block_index}_{chunk.chunk_index}.pchk"
        except ValueError:
            filename = f"{uuid.uuid4().hex}.pchk"

        payload = {
            "title": f"picard-{filename}",
            "file_name": filename,
            "content": encoded_chunk,
            "visibility": "private",
        }

        session = await self._get_session()
        async with session.post(
            f"{self._url}/api/v4/snippets",
            json=payload,
            headers=self._headers(),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        snippet_id = data["id"]
        return StoredChunkRef(
            adapter_name="gitlab-snippet", location=f"glsnip://{snippet_id}"
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        snippet_id = ref.location.split("://", 1)[1]

        session = await self._get_session()
        async with session.get(
            f"{self._url}/api/v4/snippets/{snippet_id}/raw",
            headers=self._headers(),
        ) as resp:
            resp.raise_for_status()
            return await resp.text()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        snippet_id = ref.location.split("://", 1)[1]

        try:
            session = await self._get_session()
            async with session.delete(
                f"{self._url}/api/v4/snippets/{snippet_id}",
                headers=self._headers(),
            ) as resp:
                return resp.status == 204
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        try:
            session = await self._get_session()
            async with session.get(
                f"{self._url}/api/v4/snippets",
                headers=self._headers(),
            ) as resp:
                resp.raise_for_status()
                snippets = await resp.json()

            for s in snippets:
                if manifest_id in s.get("title", ""):
                    refs.append(
                        StoredChunkRef(
                            adapter_name="gitlab-snippet",
                            location=f"glsnip://{s['id']}",
                        )
                    )
        except Exception:
            pass
        return refs

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get(
                f"{self._url}/api/v4/user", headers=self._headers()
            ) as resp:
                return resp.status == 200
        except Exception:
            return False
