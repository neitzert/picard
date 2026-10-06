"""GitHub Gist adapter — stores chunks as files in secret gists.

Each chunk becomes a single-file gist.  The filename encodes the chunk
identity so gists can be discovered for recovery.

Requires: pip install picard[github]
"""

from __future__ import annotations

from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_API_BASE = "https://api.github.com"


def _pack_location(gist_id: str, filename: str) -> str:
    return f"{gist_id}:{filename}"


def _unpack_location(location: str) -> tuple[str, str]:
    gist_id, filename = location.split(":", 1)
    return gist_id, filename


class GitHubAdapter(Adapter):
    """Scatter chunks into GitHub secret gists."""

    def __init__(
        self,
        token: str,
        *,
        gist_description: str = "picard-chunks",
        max_chunk_bytes: int = 900_000,
    ) -> None:
        self._token = token
        self._gist_description = gist_description
        self._max = max_chunk_bytes
        self._client: object | None = None

    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        import httpx

        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"token {self._token}",
                "Accept": "application/vnd.github.v3+json",
            },
            timeout=30.0,
        )
        return self._client

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="github",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    @staticmethod
    def _chunk_filename(manifest_id: str, block: int, chunk_idx: int) -> str:
        return f"pchk_{manifest_id}_b{block}_c{chunk_idx}.txt"

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        client = await self._ensure_client()
        chunk = Chunk.decode(encoded_chunk)
        filename = self._chunk_filename(
            chunk.manifest_id, chunk.block_index, chunk.chunk_index,
        )
        resp = await client.post(
            f"{_API_BASE}/gists",
            json={
                "description": self._gist_description,
                "public": False,
                "files": {filename: {"content": encoded_chunk}},
            },
        )
        resp.raise_for_status()
        gist_id = resp.json()["id"]
        return StoredChunkRef(
            adapter_name="github",
            location=_pack_location(gist_id, filename),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        client = await self._ensure_client()
        gist_id, filename = _unpack_location(ref.location)
        resp = await client.get(f"{_API_BASE}/gists/{gist_id}")
        resp.raise_for_status()
        return resp.json()["files"][filename]["content"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        client = await self._ensure_client()
        gist_id, _ = _unpack_location(ref.location)
        try:
            resp = await client.delete(f"{_API_BASE}/gists/{gist_id}")
            return resp.status_code == 204
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._ensure_client()
        prefix = f"pchk_{manifest_id}_"
        refs: list[StoredChunkRef] = []
        page = 1
        while True:
            resp = await client.get(
                f"{_API_BASE}/gists",
                params={"per_page": 100, "page": page},
            )
            resp.raise_for_status()
            gists = resp.json()
            if not gists:
                break
            for gist in gists:
                if gist.get("description") != self._gist_description:
                    continue
                for fname in gist.get("files", {}):
                    if fname.startswith(prefix):
                        refs.append(StoredChunkRef(
                            adapter_name="github",
                            location=_pack_location(gist["id"], fname),
                        ))
            page += 1
        return refs

    async def health_check(self) -> bool:
        try:
            client = await self._ensure_client()
            resp = await client.get(f"{_API_BASE}/user")
            return resp.status_code == 200
        except Exception:
            return False
