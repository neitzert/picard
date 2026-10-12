"""RubyGems adapter -- stores chunks as gem descriptions.

Each chunk becomes a gem whose summary/description holds the data.

WARNING: Use only with a private gem server or your own rubygems.org
account. Publishing stub gems to public rubygems.org violates their ToS.

Requires: pip install picard[rubygems]  (aiohttp)
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class RubyGemsAdapter(Adapter):
    """Scatter chunks into RubyGems package descriptions."""

    def __init__(
        self,
        api_key: str,
        *,
        registry_url: str = "https://rubygems.org",
        gem_prefix: str = "picard-chunk",
        max_chunk_bytes: int = 32768,
        max_posts: int = 10,
    ) -> None:
        self._api_key = api_key
        self._registry = registry_url.rstrip("/")
        self._prefix = gem_prefix
        self._max = max_chunk_bytes
        self._max_posts = max_posts
        self._post_count = 0
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
            name="rubygems",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=10,
        )

    def _gem_name(self, manifest_id: str, block: int, chunk: int) -> str:
        slug = hashlib.sha256(f"{manifest_id}/{block}/{chunk}".encode()).hexdigest()[:12]
        return f"{self._prefix}-{slug}"

    def _headers(self) -> dict:
        return {"Authorization": self._api_key}

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        if self._post_count >= self._max_posts:
            raise RuntimeError(
                f"RubyGems session post cap reached ({self._max_posts}); "
                "raise max_posts or use a private gem server"
            )
        try:
            chunk = Chunk.decode(encoded_chunk)
            gem_name = self._gem_name(chunk.manifest_id, chunk.block_index, chunk.chunk_index)
        except ValueError:
            gem_name = f"{self._prefix}-{uuid.uuid4().hex[:12]}"

        version = "0.0.1"

        body = {
            "name": gem_name,
            "version": version,
            "summary": encoded_chunk[:1024],
            "description": encoded_chunk,
            "authors": "picard",
        }

        session = await self._get_session()
        async with session.post(
            f"{self._registry}/api/v1/gems",
            headers=self._headers(),
            json=body,
        ) as resp:
            resp.raise_for_status()

        loc = f"gems://{gem_name}/{version}"
        self._post_count += 1
        return StoredChunkRef(adapter_name="rubygems", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        parts = ref.location.removeprefix("gems://").split("/")
        gem_name = parts[0]

        session = await self._get_session()
        async with session.get(
            f"{self._registry}/api/v1/gems/{gem_name}.json",
            headers=self._headers(),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        desc = data.get("info", "")
        if not desc:
            raise KeyError(f"chunk not found: {ref.location}")
        return desc

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        parts = ref.location.removeprefix("gems://").split("/")
        gem_name = parts[0]
        version = parts[1] if len(parts) > 1 else "0.0.1"

        try:
            session = await self._get_session()
            async with session.delete(
                f"{self._registry}/api/v1/gems/yank",
                headers=self._headers(),
                params={"gem_name": gem_name, "version": version},
            ) as resp:
                return resp.status < 400
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        try:
            session = await self._get_session()
            async with session.get(
                f"{self._registry}/api/v1/search.json",
                params={"query": self._prefix},
                headers=self._headers(),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

            refs = []
            for gem in data:
                name = gem.get("name", "")
                if name.startswith(self._prefix):
                    refs.append(StoredChunkRef(
                        adapter_name="rubygems",
                        location=f"gems://{name}/{gem.get('version', '0.0.1')}",
                    ))
            return refs
        except Exception:
            return []

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get(
                f"{self._registry}/api/v1/api_key.json",
                headers=self._headers(),
            ) as resp:
                return resp.status < 400
        except Exception:
            return False
