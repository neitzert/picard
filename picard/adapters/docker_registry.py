"""Docker/OCI registry adapter — stores chunks as image layer blobs.

Each chunk becomes a blob in an OCI-compliant registry:
    oci://{registry}/{repo}@sha256:{digest}

Requires: pip install picard[docker]
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class DockerRegistryAdapter(Adapter):
    """Scatter chunks into an OCI-compliant container registry."""

    def __init__(
        self,
        registry_url: str,
        *,
        repository: str = "picard/chunks",
        username: str | None = None,
        password: str | None = None,
        max_chunk_bytes: int = 10_485_760,
    ) -> None:
        self._url = registry_url.rstrip("/")
        self._repo = repository
        self._username = username
        self._password = password
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="docker-registry",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=120,
        )

    def _auth(self):
        import aiohttp

        if self._username and self._password:
            return aiohttp.BasicAuth(self._username, self._password)
        return None

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import aiohttp

        data = encoded_chunk.encode()
        digest = f"sha256:{hashlib.sha256(data).hexdigest()}"

        async with aiohttp.ClientSession(auth=self._auth()) as session:
            # Start upload
            url = f"{self._url}/v2/{self._repo}/blobs/uploads/"
            async with session.post(url) as resp:
                resp.raise_for_status()
                upload_url = resp.headers.get("Location", "")
                if not upload_url.startswith("http"):
                    upload_url = f"{self._url}{upload_url}"

            # Complete upload with digest
            sep = "&" if "?" in upload_url else "?"
            put_url = f"{upload_url}{sep}digest={digest}"
            async with session.put(
                put_url,
                data=data,
                headers={"Content-Type": "application/octet-stream"},
            ) as resp:
                resp.raise_for_status()

        location = f"oci://{self._url.split('://', 1)[-1]}/{self._repo}@{digest}"
        return StoredChunkRef(adapter_name="docker-registry", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import aiohttp

        digest = ref.location.split("@", 1)[1]
        url = f"{self._url}/v2/{self._repo}/blobs/{digest}"

        async with aiohttp.ClientSession(auth=self._auth()) as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                data = await resp.read()
        return data.decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        import aiohttp

        digest = ref.location.split("@", 1)[1]
        url = f"{self._url}/v2/{self._repo}/blobs/{digest}"

        try:
            async with aiohttp.ClientSession(auth=self._auth()) as session:
                async with session.delete(url) as resp:
                    return resp.status in (200, 202, 204)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        import aiohttp

        try:
            async with aiohttp.ClientSession(auth=self._auth()) as session:
                async with session.get(f"{self._url}/v2/") as resp:
                    return resp.status in (200, 401)
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        return {
            "used_bytes": None,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": None,
        }
