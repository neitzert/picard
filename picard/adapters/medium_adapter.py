"""Medium adapter — stores chunks as draft stories.

Uses the Medium API to create draft posts.  Medium's API is
write-heavy (no read-back for drafts), so a local cache supplements
the write confirmation for retrieval.

Requires: pip install picard[medium]   (uses aiohttp)
"""

from __future__ import annotations

from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_API_BASE = "https://api.medium.com/v1"


class MediumAdapter(Adapter):
    """Scatter chunks into Medium draft stories."""

    def __init__(
        self,
        integration_token: str,
        author_id: str,
        *,
        max_chunk_bytes: int = 65536,
    ) -> None:
        self._token = integration_token
        self._author_id = author_id
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="medium",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=10,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_API_BASE}/users/{self._author_id}/posts",
                headers=self._headers(),
                json={
                    "title": "pchk",
                    "contentFormat": "markdown",
                    "content": encoded_chunk,
                    "publishStatus": "draft",
                },
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                post_id = data.get("data", {}).get("id", "")
        loc = f"medium://{self._author_id}/{post_id}"
        self._cache[loc] = encoded_chunk
        return StoredChunkRef(adapter_name="medium", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        cached = self._cache.get(ref.location)
        if cached:
            return cached
        raise KeyError(f"medium draft not in cache: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        for loc, data in self._cache.items():
            if manifest_id in data:
                refs.append(StoredChunkRef(adapter_name="medium", location=loc))
        return refs

    async def health_check(self) -> bool:
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{_API_BASE}/me", headers=self._headers()
                ) as resp:
                    return resp.status == 200
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
