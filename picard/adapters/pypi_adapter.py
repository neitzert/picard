"""PyPI adapter -- stores chunks as Python package descriptions.

Each chunk becomes a PyPI package whose long_description holds the
encoded chunk data.  Reads via the JSON API.

WARNING: Use only with a private index or your own PyPI account.
Publishing stub packages to public PyPI violates PyPI's ToS.

Requires: pip install picard[pypi]  (aiohttp)
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk
import aiohttp


class PypiAdapter(Adapter):
    """Scatter chunks into PyPI package descriptions."""

    def __init__(
        self,
        username: str = "__token__",
        password: str = "",
        *,
        repository_url: str = "https://upload.pypi.org/legacy/",
        index_url: str = "https://pypi.org",
        pkg_prefix: str = "picard-chunk",
        max_chunk_bytes: int = 65536,
        max_posts: int = 10,
    ) -> None:
        self._username = username
        self._password = password
        self._repo_url = repository_url.rstrip("/")
        self._index_url = index_url.rstrip("/")
        self._prefix = pkg_prefix
        self._max = max_chunk_bytes
        self._max_posts = max_posts
        self._post_count = 0
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
            name="pypi",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=10,
        )

    def _pkg_name(self, manifest_id: str, block: int, chunk: int) -> str:
        slug = hashlib.sha256(f"{manifest_id}/{block}/{chunk}".encode()).hexdigest()[:12]
        return f"{self._prefix}-{slug}"

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        if self._post_count >= self._max_posts:
            raise RuntimeError(
                f"PyPI session post cap reached ({self._max_posts}); "
                "raise max_posts or use a private index"
            )
        try:
            chunk = Chunk.decode(encoded_chunk)
            pkg_name = self._pkg_name(chunk.manifest_id, chunk.block_index, chunk.chunk_index)
        except ValueError:
            pkg_name = f"{self._prefix}-{uuid.uuid4().hex[:12]}"

        version = "0.0.1"
        auth = aiohttp.BasicAuth(self._username, self._password)

        form = aiohttp.FormData()
        form.add_field(":action", "file_upload")
        form.add_field("protocol_version", "1")
        form.add_field("name", pkg_name)
        form.add_field("version", version)
        form.add_field("description", encoded_chunk)
        form.add_field("description_content_type", "text/plain")
        form.add_field("filetype", "sdist")

        async with aiohttp.ClientSession(auth=auth) as session:
            async with session.post(self._repo_url, data=form) as resp:
                resp.raise_for_status()

        loc = f"pypi://{pkg_name}/{version}"
        self._post_count += 1
        return StoredChunkRef(adapter_name="pypi", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        parts = ref.location.removeprefix("pypi://").split("/")
        pkg_name = parts[0]

        session = await self._get_session()
        async with session.get(
            f"{self._index_url}/pypi/{pkg_name}/json"
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        desc = data.get("info", {}).get("description", "")
        if not desc:
            raise KeyError(f"chunk not found: {ref.location}")
        return desc

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        parts = ref.location.removeprefix("pypi://").split("/")
        pkg_name = parts[0]
        version = parts[1] if len(parts) > 1 else "0.0.1"
        auth = aiohttp.BasicAuth(self._username, self._password)

        try:
            async with aiohttp.ClientSession(auth=auth) as session:
                async with session.delete(
                    f"{self._index_url}/pypi/{pkg_name}/{version}"
                ) as resp:
                    return resp.status < 400
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        try:
            session = await self._get_session()
            async with session.get(
                f"{self._index_url}/simple/",
                headers={"Accept": "application/vnd.pypi.simple.v1+json"},
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

            refs = []
            for project in data.get("projects", []):
                name = project.get("name", "")
                if name.startswith(self._prefix):
                    refs.append(StoredChunkRef(
                        adapter_name="pypi",
                        location=f"pypi://{name}/0.0.1",
                    ))
            return refs
        except Exception:
            return []

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get(
                f"{self._index_url}/pypi/pip/json"
            ) as resp:
                return resp.status == 200
        except Exception:
            return False
