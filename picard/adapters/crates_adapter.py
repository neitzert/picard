"""crates.io adapter -- stores chunks as Rust crate descriptions.

Each chunk becomes a crate whose description field holds the data.

WARNING: Use only with a private registry or your own crates.io account.
Publishing stub crates to public crates.io violates their ToS.

Requires: pip install picard[crates]  (aiohttp)
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class CratesAdapter(Adapter):
    """Scatter chunks into crates.io crate descriptions."""

    def __init__(
        self,
        token: str,
        *,
        registry_url: str = "https://crates.io",
        crate_prefix: str = "picard-chunk",
        max_chunk_bytes: int = 32768,
        max_posts: int = 10,
    ) -> None:
        self._token = token
        self._registry = registry_url.rstrip("/")
        self._prefix = crate_prefix
        self._max = max_chunk_bytes
        self._max_posts = max_posts
        self._post_count = 0
        self._session = None

    async def _get_session(self):
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="crates-io",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=10,
        )

    def _crate_name(self, manifest_id: str, block: int, chunk: int) -> str:
        slug = hashlib.sha256(f"{manifest_id}/{block}/{chunk}".encode()).hexdigest()[:12]
        return f"{self._prefix}-{slug}"

    def _headers(self) -> dict:
        return {"Authorization": self._token}

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        if self._post_count >= self._max_posts:
            raise RuntimeError(
                f"crates.io session post cap reached ({self._max_posts}); "
                "raise max_posts or use a private registry"
            )
        import aiohttp

        try:
            chunk = Chunk.decode(encoded_chunk)
            crate_name = self._crate_name(chunk.manifest_id, chunk.block_index, chunk.chunk_index)
        except ValueError:
            crate_name = f"{self._prefix}-{uuid.uuid4().hex[:12]}"

        version = "0.1.0"

        body = {
            "name": crate_name,
            "vers": version,
            "description": encoded_chunk,
            "deps": [],
            "features": {},
            "license": "MIT",
        }

        session = await self._get_session()
        async with session.put(
            f"{self._registry}/api/v1/crates/new",
            headers=self._headers(),
            json=body,
        ) as resp:
            resp.raise_for_status()

        loc = f"crates://{crate_name}/{version}"
        self._post_count += 1
        return StoredChunkRef(adapter_name="crates-io", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        parts = ref.location.removeprefix("crates://").split("/")
        crate_name = parts[0]

        session = await self._get_session()
        async with session.get(
            f"{self._registry}/api/v1/crates/{crate_name}",
            headers=self._headers(),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        desc = data.get("crate", {}).get("description", "")
        if not desc:
            raise KeyError(f"chunk not found: {ref.location}")
        return desc

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        parts = ref.location.removeprefix("crates://").split("/")
        crate_name = parts[0]
        version = parts[1] if len(parts) > 1 else "0.1.0"

        try:
            session = await self._get_session()
            async with session.delete(
                f"{self._registry}/api/v1/crates/{crate_name}/{version}/yank",
                headers=self._headers(),
            ) as resp:
                return resp.status < 400
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        try:
            session = await self._get_session()
            async with session.get(
                f"{self._registry}/api/v1/crates",
                params={"q": self._prefix, "per_page": 100},
                headers=self._headers(),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

            refs = []
            for crate in data.get("crates", []):
                name = crate.get("id", "")
                if name.startswith(self._prefix):
                    refs.append(StoredChunkRef(
                        adapter_name="crates-io",
                        location=f"crates://{name}/{crate.get('max_version', '0.1.0')}",
                    ))
            return refs
        except Exception:
            return []

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get(
                f"{self._registry}/api/v1/summary",
                headers=self._headers(),
            ) as resp:
                return resp.status == 200
        except Exception:
            return False
