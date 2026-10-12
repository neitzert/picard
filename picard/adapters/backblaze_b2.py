"""Backblaze B2 adapter — stores chunks as B2 objects.

Each chunk becomes an object in a B2 bucket. Uses B2's native API
(authorize_account, get_upload_url, upload_file) for writes and
download_file_by_name for reads.

Requires: pip install picard-store[b2]   (aiohttp)
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Sequence

import aiohttp

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_AUTH_URL = "https://api.backblazeb2.com/b2api/v2/b2_authorize_account"


class BackblazeB2Adapter(Adapter):
    """Scatter chunks into Backblaze B2."""

    def __init__(
        self,
        application_key_id: str,
        application_key: str,
        bucket_name: str,
        *,
        prefix: str = "picard/",
        max_chunk_bytes: int = 5_368_709_120,
    ) -> None:
        self._key_id = application_key_id
        self._key = application_key
        self._bucket_name = bucket_name
        self._prefix = prefix
        self._max = max_chunk_bytes
        self._auth: dict | None = None
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    def _obj_name(self, manifest_id: str, block: int, chunk_index: int) -> str:
        return f"{self._prefix}{manifest_id}/b{block}_c{chunk_index}.pchk"

    async def _authorize(self) -> dict:
        if self._auth is not None:
            return self._auth
        auth = aiohttp.BasicAuth(self._key_id, self._key)
        session = await self._get_session()
        async with session.get(_AUTH_URL, auth=auth) as resp:
            resp.raise_for_status()
            self._auth = await resp.json()
        return self._auth

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="backblaze-b2",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=500,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            name = self._obj_name(
                chunk.manifest_id, chunk.block_index, chunk.chunk_index
            )
        except ValueError:
            name = f"{self._prefix}{uuid.uuid4().hex}.pchk"

        auth = await self._authorize()
        api_url = auth["apiUrl"]
        bucket_id = auth.get("allowed", {}).get("bucketId", "")
        token = auth["authorizationToken"]

        session = await self._get_session()
        async with session.post(
            f"{api_url}/b2api/v2/b2_get_upload_url",
            json={"bucketId": bucket_id},
            headers={"Authorization": token},
        ) as resp:
            resp.raise_for_status()
            upload_info = await resp.json()

        body = encoded_chunk.encode()
        sha1 = hashlib.sha1(body).hexdigest()
        async with session.post(
            upload_info["uploadUrl"],
            data=body,
            headers={
                "Authorization": upload_info["authorizationToken"],
                "X-Bz-File-Name": name,
                "Content-Type": "text/plain",
                "Content-Length": str(len(body)),
                "X-Bz-Content-Sha1": sha1,
            },
        ) as resp:
            resp.raise_for_status()

        return StoredChunkRef(adapter_name="backblaze-b2", location=name)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        auth = await self._authorize()
        download_url = auth["downloadUrl"]
        token = auth["authorizationToken"]

        session = await self._get_session()
        url = f"{download_url}/file/{self._bucket_name}/{ref.location}"
        async with session.get(
            url, headers={"Authorization": token}
        ) as resp:
            resp.raise_for_status()
            return await resp.text()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        auth = await self._authorize()
        api_url = auth["apiUrl"]
        token = auth["authorizationToken"]

        try:
            session = await self._get_session()
            async with session.post(
                f"{api_url}/b2api/v2/b2_hide_file",
                json={
                    "bucketId": auth.get("allowed", {}).get("bucketId", ""),
                    "fileName": ref.location,
                },
                headers={"Authorization": token},
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        auth = await self._authorize()
        api_url = auth["apiUrl"]
        token = auth["authorizationToken"]
        prefix = f"{self._prefix}{manifest_id}/"
        refs: list[StoredChunkRef] = []

        session = await self._get_session()
        async with session.post(
            f"{api_url}/b2api/v2/b2_list_file_names",
            json={
                "bucketId": auth.get("allowed", {}).get("bucketId", ""),
                "prefix": prefix,
                "maxFileCount": 10000,
            },
            headers={"Authorization": token},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            for f in data.get("files", []):
                refs.append(
                    StoredChunkRef(
                        adapter_name="backblaze-b2", location=f["fileName"]
                    )
                )

        return refs

    async def health_check(self) -> bool:
        try:
            await self._authorize()
            return True
        except Exception:
            return False
