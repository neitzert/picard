"""Google Drive adapter — stores chunks as Drive files or appProperties.

Each chunk becomes a file in Google Drive, optionally inside a specific
folder. Data can also be hidden in appProperties (invisible to users in
the Drive UI).

Requires: pip install picard[gdrive]   (aiohttp)
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Sequence

import aiohttp

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_API = "https://www.googleapis.com"


class GoogleDriveAdapter(Adapter):
    """Scatter chunks into Google Drive."""

    def __init__(
        self,
        credentials_json: str | None = None,
        *,
        folder_id: str | None = None,
        prefix: str = "picard-",
        max_chunk_bytes: int = 5_242_880,
    ) -> None:
        self._credentials_json = credentials_json
        self._folder_id = folder_id
        self._prefix = prefix
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

    def _get_headers(self) -> dict[str, str]:
        try:
            import google.auth
            import google.auth.transport.requests

            creds, _ = google.auth.default()
            creds.refresh(google.auth.transport.requests.Request())
            return {"Authorization": f"Bearer {creds.token}"}
        except Exception:
            return {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="google-drive",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=60,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            name = (
                f"{self._prefix}{chunk.manifest_id}_"
                f"b{chunk.block_index}_c{chunk.chunk_index}.pchk"
            )
        except ValueError:
            name = f"{self._prefix}{uuid.uuid4().hex}.pchk"

        headers = await asyncio.to_thread(self._get_headers)

        metadata: dict = {"name": name, "mimeType": "text/plain"}
        if self._folder_id:
            metadata["parents"] = [self._folder_id]

        boundary = "picard_boundary"
        body = (
            f"--{boundary}\r\n"
            f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(metadata)}\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: text/plain\r\n\r\n"
            f"{encoded_chunk}\r\n"
            f"--{boundary}--"
        )

        headers["Content-Type"] = f"multipart/related; boundary={boundary}"

        session = await self._get_session()
        async with session.post(
            f"{_API}/upload/drive/v3/files?uploadType=multipart",
            data=body.encode(),
            headers=headers,
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            file_id = data["id"]

        return StoredChunkRef(adapter_name="google-drive", location=file_id)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        headers = await asyncio.to_thread(self._get_headers)

        session = await self._get_session()
        async with session.get(
            f"{_API}/drive/v3/files/{ref.location}?alt=media",
            headers=headers,
        ) as resp:
            resp.raise_for_status()
            return await resp.text()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        headers = await asyncio.to_thread(self._get_headers)

        try:
            session = await self._get_session()
            async with session.delete(
                f"{_API}/drive/v3/files/{ref.location}",
                headers=headers,
            ) as resp:
                return resp.status in (200, 204)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        headers = await asyncio.to_thread(self._get_headers)
        query = f"name contains '{self._prefix}{manifest_id}_'"
        if self._folder_id:
            query += f" and '{self._folder_id}' in parents"
        refs: list[StoredChunkRef] = []

        session = await self._get_session()
        async with session.get(
            f"{_API}/drive/v3/files",
            params={"q": query, "fields": "files(id,name)"},
            headers=headers,
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            for f in data.get("files", []):
                refs.append(
                    StoredChunkRef(adapter_name="google-drive", location=f["id"])
                )

        return refs

    async def health_check(self) -> bool:
        headers = await asyncio.to_thread(self._get_headers)

        try:
            session = await self._get_session()
            async with session.get(
                f"{_API}/drive/v3/about?fields=user",
                headers=headers,
            ) as resp:
                return resp.status == 200
        except Exception:
            return False
