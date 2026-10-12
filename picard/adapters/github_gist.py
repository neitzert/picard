"""GitHub Gist adapter — stores chunks as secret (unlisted) gists.

Each chunk becomes a gist file:
    gist://{gist_id}

Requires: pip install picard[gist]
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_API = "https://api.github.com"


class GitHubGistAdapter(Adapter):
    """Scatter chunks into secret GitHub Gists."""

    def __init__(
        self,
        token: str,
        *,
        max_chunk_bytes: int = 10_485_760,
    ) -> None:
        self._token = token
        self._max = max_chunk_bytes
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
            name="github-gist",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    def _headers(self) -> dict:
        return {
            "Authorization": f"token {self._token}",
            "Accept": "application/vnd.github+json",
        }

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            filename = f"{chunk.manifest_id}_{chunk.block_index}_{chunk.chunk_index}.pchk"
        except ValueError:
            filename = f"{uuid.uuid4().hex}.pchk"

        payload = {
            "description": "picard chunk",
            "public": False,
            "files": {filename: {"content": encoded_chunk}},
        }

        session = await self._get_session()
        async with session.post(
            f"{_API}/gists", json=payload, headers=self._headers()
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        gist_id = data["id"]
        return StoredChunkRef(adapter_name="github-gist", location=f"gist://{gist_id}")

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        gist_id = ref.location.split("://", 1)[1]

        session = await self._get_session()
        async with session.get(
            f"{_API}/gists/{gist_id}", headers=self._headers()
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        files = data["files"]
        first_file = next(iter(files.values()))
        return first_file["content"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        gist_id = ref.location.split("://", 1)[1]

        try:
            session = await self._get_session()
            async with session.delete(
                f"{_API}/gists/{gist_id}", headers=self._headers()
            ) as resp:
                return resp.status == 204
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        try:
            session = await self._get_session()
            async with session.get(
                f"{_API}/gists", headers=self._headers()
            ) as resp:
                resp.raise_for_status()
                gists = await resp.json()

            for gist in gists:
                for fname in gist.get("files", {}):
                    if fname.startswith(manifest_id):
                        refs.append(
                            StoredChunkRef(
                                adapter_name="github-gist",
                                location=f"gist://{gist['id']}",
                            )
                        )
                        break
        except Exception:
            pass
        return refs

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get(
                f"{_API}/user", headers=self._headers()
            ) as resp:
                return resp.status == 200
        except Exception:
            return False
