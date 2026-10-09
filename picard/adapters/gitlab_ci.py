"""GitLab CI adapter — stores chunks as job artifacts.

Hides data in CI pipeline build artifacts. Looks like normal
build output in the project's CI/CD section.

Requires: pip install picard[gitlab-ci]  (aiohttp)
"""

from __future__ import annotations

import base64
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk
import aiohttp


class GitLabCIAdapter(Adapter):
    """Scatter chunks into GitLab CI job artifacts."""

    def __init__(
        self,
        gitlab_url: str,
        private_token: str,
        project_id: int | str,
        *,
        max_chunk_bytes: int = 1_048_576,
    ) -> None:
        self._url = gitlab_url.rstrip("/")
        self._token = private_token
        self._project_id = project_id
        self._max = max_chunk_bytes
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
            name="gitlab-ci",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    def _headers(self) -> dict[str, str]:
        return {"PRIVATE-TOKEN": self._token}

    def _api(self) -> str:
        return f"{self._url}/api/v4/projects/{self._project_id}"

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            artifact_name = f"picard-{chunk.manifest_id}-b{chunk.block_index}-c{chunk.chunk_index}"
        except ValueError:
            artifact_name = f"picard-{uuid.uuid4().hex}"

        url = f"{self._api()}/uploads"
        data = aiohttp.FormData()
        data.add_field(
            "file",
            encoded_chunk.encode(),
            filename=f"{artifact_name}.pchk",
            content_type="application/octet-stream",
        )

        session = await self._get_session()
        async with session.post(
            url, data=data, headers=self._headers()
        ) as resp:
            resp.raise_for_status()
            body = await resp.json()

        upload_url = body.get("url", body.get("full_path", artifact_name))
        location = f"glci://{self._project_id}/{upload_url}"
        return StoredChunkRef(adapter_name="gitlab-ci", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        parts = ref.location.split("/", 2)
        path = parts[2] if len(parts) > 2 else parts[-1]
        url = f"{self._url}{path}" if path.startswith("/") else f"{self._api()}/{path}"

        session = await self._get_session()
        async with session.get(url, headers=self._headers()) as resp:
            resp.raise_for_status()
            raw = await resp.read()

        return raw.decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        url = f"{self._api()}"
        try:
            session = await self._get_session()
            async with session.get(url, headers=self._headers()) as resp:
                return resp.status == 200
        except Exception:
            return False
