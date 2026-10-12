"""0x0.st adapter — stores chunks as uploaded files.

Each chunk becomes a hosted file:
    0x0://{file_url}

Notice: 0x0.st is a public community-operated file host.  Only use this
adapter with a self-hosted 0x0 instance or with explicit permission from
the instance operator.  Uploading bulk data to the public instance violates
its abuse policy.

Requires: aiohttp
"""

from __future__ import annotations

from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
import aiohttp


class NullPointerAdapter(Adapter):
    """Scatter chunks into 0x0.st file uploads — zero signup required."""

    def __init__(
        self,
        *,
        url: str = "https://0x0.st",
        max_chunk_bytes: int = 536_870_912,
        max_posts: int = 10,
    ) -> None:
        self._url = url.rstrip("/")
        self._max = max_chunk_bytes
        self._session = None
        self._max_posts = max_posts
        self._post_count = 0


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
            name="0x0",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=10,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        if self._post_count >= self._max_posts:
            raise RuntimeError(
                f"0x0 adapter reached max_posts={self._max_posts} per session"
            )
        form = aiohttp.FormData()
        form.add_field(
            "file",
            encoded_chunk.encode(),
            filename="chunk.pchk",
            content_type="application/octet-stream",
        )

        session = await self._get_session()
        async with session.post(self._url, data=form) as resp:
            resp.raise_for_status()
            file_url = (await resp.text()).strip()

        self._post_count += 1
        return StoredChunkRef(adapter_name="0x0", location=f"0x0://{file_url}")

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        file_url = ref.location.split("://", 1)[1]

        session = await self._get_session()
        async with session.get(file_url) as resp:
            resp.raise_for_status()
            return await resp.text()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.head(self._url) as resp:
                return resp.status == 200
        except Exception:
            return False
