"""GitHub Actions adapter — stores chunks as workflow artifacts.

Hides data in CI/CD build artifacts with 90-day default retention.
Looks like normal build output to anyone reviewing the repo.

Requires: pip install picard[github-actions]  (aiohttp)
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_API = "https://api.github.com"


class GitHubActionsAdapter(Adapter):
    """Scatter chunks into GitHub Actions artifacts."""

    def __init__(
        self,
        token: str,
        owner: str,
        repo: str,
        *,
        api_url: str = _API,
        max_chunk_bytes: int = 1_048_576,
    ) -> None:
        self._token = token
        self._owner = owner
        self._repo = repo
        self._api_url = api_url.rstrip("/")
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="github-actions",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _repo_url(self) -> str:
        return f"{self._api_url}/repos/{self._owner}/{self._repo}"

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import aiohttp

        try:
            chunk = Chunk.decode(encoded_chunk)
            artifact_name = f"picard-{chunk.manifest_id}-b{chunk.block_index}-c{chunk.chunk_index}"
        except ValueError:
            artifact_name = f"picard-{uuid.uuid4().hex}"

        url = f"{self._repo_url()}/actions/artifacts"
        payload = {
            "name": artifact_name,
            "data": base64.b64encode(encoded_chunk.encode()).decode(),
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, headers=self._headers()
            ) as resp:
                resp.raise_for_status()
                body = await resp.json()

        artifact_id = body.get("id", artifact_name)
        location = f"gha://{self._owner}/{self._repo}/artifacts/{artifact_id}"
        return StoredChunkRef(adapter_name="github-actions", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import aiohttp

        artifact_id = ref.location.rsplit("/", 1)[-1]
        url = f"{self._repo_url()}/actions/artifacts/{artifact_id}/zip"

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self._headers()) as resp:
                resp.raise_for_status()
                raw = await resp.read()

        return base64.b64decode(raw).decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        import aiohttp

        artifact_id = ref.location.rsplit("/", 1)[-1]
        url = f"{self._repo_url()}/actions/artifacts/{artifact_id}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.delete(url, headers=self._headers()) as resp:
                    return resp.status in (200, 204)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        import aiohttp

        url = f"{self._repo_url()}/actions/artifacts"
        params = {"per_page": 100, "name": f"picard-{manifest_id}"}

        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, params=params, headers=self._headers()
            ) as resp:
                resp.raise_for_status()
                body = await resp.json()

        refs: list[StoredChunkRef] = []
        for a in body.get("artifacts", []):
            if a["name"].startswith(f"picard-{manifest_id}"):
                loc = f"gha://{self._owner}/{self._repo}/artifacts/{a['id']}"
                refs.append(StoredChunkRef(adapter_name="github-actions", location=loc))
        return refs

    async def health_check(self) -> bool:
        import aiohttp

        url = f"{self._repo_url()}/actions/runs"
        params = {"per_page": 1}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, params=params, headers=self._headers()
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False
