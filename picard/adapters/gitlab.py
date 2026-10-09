"""GitLab adapter — stores chunks as GitLab snippets.

Each chunk becomes a private snippet whose title encodes the manifest,
block, and chunk indices for discovery.  The snippet body holds the
PCHK-encoded string.

Requires: pip install picard[gitlab]   (httpx)
"""

from __future__ import annotations

from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class GitLabAdapter(Adapter):
    """Scatter chunks into GitLab snippets via the REST API."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://gitlab.com",
        max_chunk_bytes: int = 900_000,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._max = max_chunk_bytes
        self._client: object | None = None

    @property
    def _api(self) -> str:
        return f"{self._base_url}/api/v4"

    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        import httpx

        self._client = httpx.AsyncClient(
            headers={"PRIVATE-TOKEN": self._token},
            timeout=30.0,
        )
        return self._client

    @staticmethod
    def _snippet_title(manifest_id: str, block: int, chunk_idx: int) -> str:
        return f"pchk_{manifest_id}_b{block}_c{chunk_idx}"

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="gitlab",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=60,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        client = await self._ensure_client()
        chunk = Chunk.decode(encoded_chunk)
        title = self._snippet_title(
            chunk.manifest_id, chunk.block_index, chunk.chunk_index,
        )
        resp = await client.post(
            f"{self._api}/snippets",
            json={
                "title": title,
                "visibility": "private",
                "files": [{"file_path": "chunk.txt", "content": encoded_chunk}],
            },
        )
        resp.raise_for_status()
        return StoredChunkRef(
            adapter_name="gitlab",
            location=str(resp.json()["id"]),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        client = await self._ensure_client()
        resp = await client.get(f"{self._api}/snippets/{ref.location}/raw")
        resp.raise_for_status()
        return resp.text

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        client = await self._ensure_client()
        try:
            resp = await client.delete(f"{self._api}/snippets/{ref.location}")
            return resp.status_code == 204
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._ensure_client()
        prefix = f"pchk_{manifest_id}_"
        refs: list[StoredChunkRef] = []
        page = 1
        while True:
            resp = await client.get(
                f"{self._api}/snippets",
                params={"per_page": 100, "page": page},
            )
            resp.raise_for_status()
            snippets = resp.json()
            if not snippets:
                break
            for s in snippets:
                if s.get("title", "").startswith(prefix):
                    refs.append(
                        StoredChunkRef(
                            adapter_name="gitlab",
                            location=str(s["id"]),
                        )
                    )
            page += 1
        return refs

    async def health_check(self) -> bool:
        try:
            client = await self._ensure_client()
            resp = await client.get(f"{self._api}/user")
            return resp.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
