"""npm registry adapter -- stores chunks as npm package descriptions.

Each chunk becomes a scoped npm package with the data in the
description field.  Reads via the registry JSON API.

WARNING: Use only with registries you control (private registry or your
own npm account). Publishing garbage packages to public npm violates
npm's ToS.

Requires: pip install picard-store[npm]  (aiohttp)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class NpmRegistryAdapter(Adapter):
    """Scatter chunks into npm package descriptions."""

    def __init__(
        self,
        token: str,
        *,
        registry_url: str = "https://registry.npmjs.org",
        scope: str = "@picard",
        max_chunk_bytes: int = 65536,
        max_posts: int = 10,
    ) -> None:
        self._token = token
        self._registry = registry_url.rstrip("/")
        self._scope = scope
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
            name="npm-registry",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    def _pkg_name(self, manifest_id: str, block: int, chunk: int) -> str:
        slug = hashlib.sha256(f"{manifest_id}/{block}/{chunk}".encode()).hexdigest()[:12]
        return f"{self._scope}/pchk-{slug}"

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        if self._post_count >= self._max_posts:
            raise RuntimeError(
                f"npm session post cap reached ({self._max_posts}); "
                "raise max_posts or use a private registry"
            )
        try:
            chunk = Chunk.decode(encoded_chunk)
            pkg_name = self._pkg_name(chunk.manifest_id, chunk.block_index, chunk.chunk_index)
        except ValueError:
            pkg_name = f"{self._scope}/pchk-{uuid.uuid4().hex[:12]}"

        version = "1.0.0"
        tarball = b""  # minimal empty tarball placeholder

        body = {
            "_id": pkg_name,
            "description": encoded_chunk,
            "version": version,
            "versions": {
                version: {
                    "name": pkg_name,
                    "version": version,
                    "description": encoded_chunk,
                    "dist": {"tarball": ""},
                }
            },
            "_attachments": {},
        }

        session = await self._get_session()
        async with session.put(
            f"{self._registry}/{pkg_name}",
            headers=self._headers(),
            json=body,
        ) as resp:
            resp.raise_for_status()

        loc = f"npm://{pkg_name}/{version}"
        self._post_count += 1
        return StoredChunkRef(adapter_name="npm-registry", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        # location: npm://@scope/name/version
        parts = ref.location.removeprefix("npm://").rsplit("/", 1)
        pkg_name = parts[0]

        session = await self._get_session()
        async with session.get(
            f"{self._registry}/{pkg_name}",
            headers=self._headers(),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        desc = data.get("description")
        if not desc:
            raise KeyError(f"chunk not found: {ref.location}")
        return desc

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        parts = ref.location.removeprefix("npm://").rsplit("/", 1)
        pkg_name = parts[0]

        try:
            session = await self._get_session()
            async with session.delete(
                f"{self._registry}/{pkg_name}/-rev/latest",
                headers=self._headers(),
            ) as resp:
                return resp.status < 400
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        search_text = f"pchk-"
        try:
            session = await self._get_session()
            async with session.get(
                f"{self._registry}/-/v1/search",
                params={"text": f"scope:{self._scope} {search_text}", "size": 250},
                headers=self._headers(),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

            refs = []
            for obj in data.get("objects", []):
                pkg = obj.get("package", {})
                name = pkg.get("name", "")
                version = pkg.get("version", "1.0.0")
                refs.append(StoredChunkRef(
                    adapter_name="npm-registry",
                    location=f"npm://{name}/{version}",
                ))
            return refs
        except Exception:
            return []

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get(f"{self._registry}/-/ping") as resp:
                return resp.status < 400
        except Exception:
            return False
