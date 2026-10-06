"""Dropbox adapter — stores chunks as files in Dropbox.

Each chunk becomes a file at /{root_path}/{manifest_id}/{block}_{chunk}.pchk.

Requires: pip install picard[dropbox]  (aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_API = "https://api.dropboxapi.com/2"
_CONTENT_API = "https://content.dropboxapi.com/2"


class DropboxAdapter(Adapter):
    """Scatter chunks into Dropbox files."""

    def __init__(
        self,
        access_token: str,
        *,
        root_path: str = "/picard",
        max_chunk_bytes: int = 157_286_400,
    ) -> None:
        self._token = access_token
        self._root = root_path.rstrip("/")
        self._max = max_chunk_bytes
        self._client: object | None = None

    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        import aiohttp

        self._client = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self._token}"},
        )
        return self._client

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="dropbox",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=300,
        )

    def _remote_path(self, encoded_chunk: str) -> str:
        try:
            chunk = Chunk.decode(encoded_chunk)
            return f"{self._root}/{chunk.manifest_id}/{chunk.block_index}_{chunk.chunk_index}.pchk"
        except ValueError:
            return f"{self._root}/_opaque/{uuid.uuid4().hex}.pchk"

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import json

        client = await self._ensure_client()
        path = self._remote_path(encoded_chunk)
        arg = json.dumps({"path": path, "mode": "overwrite", "autorename": False})
        async with client.post(
            f"{_CONTENT_API}/files/upload",
            headers={
                "Dropbox-API-Arg": arg,
                "Content-Type": "application/octet-stream",
            },
            data=encoded_chunk.encode(),
        ) as resp:
            resp.raise_for_status()
        loc = f"dropbox://{path}"
        return StoredChunkRef(adapter_name="dropbox", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import json

        client = await self._ensure_client()
        path = ref.location.removeprefix("dropbox://")
        arg = json.dumps({"path": path})
        async with client.post(
            f"{_CONTENT_API}/files/download",
            headers={"Dropbox-API-Arg": arg},
        ) as resp:
            resp.raise_for_status()
            return (await resp.read()).decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        client = await self._ensure_client()
        path = ref.location.removeprefix("dropbox://")
        try:
            async with client.post(
                f"{_API}/files/delete_v2",
                json={"path": path},
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._ensure_client()
        folder = f"{self._root}/{manifest_id}"
        refs: list[StoredChunkRef] = []
        try:
            async with client.post(
                f"{_API}/files/list_folder",
                json={"path": folder, "recursive": False},
            ) as resp:
                data = await resp.json()
            for entry in data.get("entries", []):
                if entry.get("name", "").endswith(".pchk"):
                    loc = f"dropbox://{entry['path_lower']}"
                    refs.append(StoredChunkRef(adapter_name="dropbox", location=loc))
        except Exception:
            pass
        return refs

    async def health_check(self) -> bool:
        try:
            client = await self._ensure_client()
            async with client.post(
                f"{_API}/users/get_current_account",
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        try:
            client = await self._ensure_client()
            async with client.post(
                f"{_API}/users/get_space_usage",
            ) as resp:
                data = await resp.json()
            used = data.get("used", 0)
            alloc = data.get("allocation", {}).get("allocated", 0)
            return {
                "used_bytes": used,
                "free_bytes": alloc - used if alloc else None,
                "total_bytes": alloc or None,
                "chunk_count": None,
            }
        except Exception:
            return {"used_bytes": None, "free_bytes": None, "total_bytes": None, "chunk_count": None}
