"""Maven repository adapter -- stores chunks as Maven artifacts.

Each chunk becomes a Maven artifact in a repository manager
(Nexus, Artifactory, or any Maven-compatible repo).

Requires: pip install picard-store[maven]  (aiohttp)
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk
import aiohttp


class MavenAdapter(Adapter):
    """Scatter chunks into a Maven repository as artifacts."""

    def __init__(
        self,
        repo_url: str,
        username: str,
        password: str,
        *,
        group_id: str = "com.picard.chunks",
        max_chunk_bytes: int = 1048576,
    ) -> None:
        self._repo_url = repo_url.rstrip("/")
        self._username = username
        self._password = password
        self._group_id = group_id
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="maven",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=60,
        )

    def _artifact_id(self, manifest_id: str, block: int, chunk: int) -> str:
        slug = hashlib.sha256(f"{manifest_id}/{block}/{chunk}".encode()).hexdigest()[:12]
        return f"pchk-{slug}"

    def _group_path(self) -> str:
        return self._group_id.replace(".", "/")

    def _artifact_url(self, artifact_id: str, version: str) -> str:
        group_path = self._group_path()
        return f"{self._repo_url}/{group_path}/{artifact_id}/{version}/{artifact_id}-{version}.dat"

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import aiohttp

        try:
            chunk = Chunk.decode(encoded_chunk)
            artifact_id = self._artifact_id(chunk.manifest_id, chunk.block_index, chunk.chunk_index)
        except ValueError:
            artifact_id = f"pchk-{uuid.uuid4().hex[:12]}"

        version = "1.0.0"
        url = self._artifact_url(artifact_id, version)
        auth = aiohttp.BasicAuth(self._username, self._password)

        async with aiohttp.ClientSession(auth=auth) as session:
            async with session.put(
                url,
                data=encoded_chunk.encode(),
                headers={"Content-Type": "application/octet-stream"},
            ) as resp:
                resp.raise_for_status()

        loc = f"maven://{self._group_id}:{artifact_id}:{version}"
        return StoredChunkRef(adapter_name="maven", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import aiohttp

        coords = ref.location.removeprefix("maven://").split(":")
        group_id, artifact_id, version = coords[0], coords[1], coords[2]
        group_path = group_id.replace(".", "/")
        url = f"{self._repo_url}/{group_path}/{artifact_id}/{version}/{artifact_id}-{version}.dat"

        auth = aiohttp.BasicAuth(self._username, self._password)

        async with aiohttp.ClientSession(auth=auth) as session:
            async with session.get(url) as resp:
                if resp.status == 404:
                    raise KeyError(f"chunk not found: {ref.location}")
                resp.raise_for_status()
                data = await resp.read()
                return data.decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        import aiohttp

        coords = ref.location.removeprefix("maven://").split(":")
        group_id, artifact_id, version = coords[0], coords[1], coords[2]
        group_path = group_id.replace(".", "/")
        url = f"{self._repo_url}/{group_path}/{artifact_id}/{version}"

        auth = aiohttp.BasicAuth(self._username, self._password)

        try:
            async with aiohttp.ClientSession(auth=auth) as session:
                async with session.delete(url) as resp:
                    return resp.status < 400
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        import aiohttp

        group_path = self._group_path()
        url = f"{self._repo_url}/{group_path}/"

        auth = aiohttp.BasicAuth(self._username, self._password)

        try:
            async with aiohttp.ClientSession(auth=auth) as session:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    text = await resp.text()

            refs = []
            for line in text.splitlines():
                if "pchk-" in line:
                    start = line.index("pchk-")
                    end = line.index("/", start) if "/" in line[start:] else len(line)
                    artifact_id = line[start:end].strip().rstrip("/")
                    refs.append(StoredChunkRef(
                        adapter_name="maven",
                        location=f"maven://{self._group_id}:{artifact_id}:1.0.0",
                    ))
            return refs
        except Exception:
            return []

    async def health_check(self) -> bool:
        import aiohttp

        auth = aiohttp.BasicAuth(self._username, self._password)

        try:
            async with aiohttp.ClientSession(auth=auth) as session:
                async with session.get(self._repo_url) as resp:
                    return resp.status < 400
        except Exception:
            return False
