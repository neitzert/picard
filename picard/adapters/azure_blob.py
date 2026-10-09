"""Azure Blob Storage adapter — stores chunks as blobs.

Each chunk becomes a block blob in an Azure Storage container at
{manifest_id}/{block}_{chunk}.pchk.

Requires: pip install picard[azure]  (aiohttp)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import uuid
from datetime import datetime, timezone
from typing import Sequence
from urllib.parse import urlparse

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class AzureBlobAdapter(Adapter):
    """Scatter chunks into Azure Blob Storage."""

    def __init__(
        self,
        account_url: str,
        *,
        sas_token: str | None = None,
        account_key: str | None = None,
        container: str = "picard",
        max_chunk_bytes: int = 5_242_880,
    ) -> None:
        self._account_url = account_url.rstrip("/")
        self._sas = sas_token
        self._account_key = account_key
        self._container = container
        self._max = max_chunk_bytes
        self._client: object | None = None

    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        import aiohttp

        self._client = aiohttp.ClientSession()
        return self._client

    def _blob_url(self, blob_name: str) -> str:
        url = f"{self._account_url}/{self._container}/{blob_name}"
        if self._sas:
            url += f"?{self._sas}"
        return url

    def _container_url(self) -> str:
        url = f"{self._account_url}/{self._container}"
        if self._sas:
            url += f"?{self._sas}"
        return url

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="azure-blob",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=1000,
        )

    def _blob_name(self, encoded_chunk: str) -> str:
        try:
            chunk = Chunk.decode(encoded_chunk)
            return f"{chunk.manifest_id}/{chunk.block_index}_{chunk.chunk_index}.pchk"
        except ValueError:
            return f"_opaque/{uuid.uuid4().hex}.pchk"

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        client = await self._ensure_client()
        name = self._blob_name(encoded_chunk)
        url = self._blob_url(name)
        headers = {
            "x-ms-blob-type": "BlockBlob",
            "Content-Type": "application/octet-stream",
        }
        async with client.put(url, data=encoded_chunk.encode(), headers=headers) as resp:
            resp.raise_for_status()
        loc = f"azblob://{self._container}/{name}"
        return StoredChunkRef(adapter_name="azure-blob", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        client = await self._ensure_client()
        name = ref.location.removeprefix(f"azblob://{self._container}/")
        url = self._blob_url(name)
        async with client.get(url) as resp:
            resp.raise_for_status()
            return (await resp.read()).decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        client = await self._ensure_client()
        name = ref.location.removeprefix(f"azblob://{self._container}/")
        url = self._blob_url(name)
        try:
            async with client.delete(url) as resp:
                return resp.status in (200, 202)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._ensure_client()
        url = self._container_url()
        sep = "&" if "?" in url else "?"
        url += f"{sep}restype=container&comp=list&prefix={manifest_id}/"
        refs: list[StoredChunkRef] = []
        try:
            async with client.get(url) as resp:
                text = await resp.text()
            import re

            for match in re.finditer(r"<Name>([^<]+)</Name>", text):
                name = match.group(1)
                loc = f"azblob://{self._container}/{name}"
                refs.append(StoredChunkRef(adapter_name="azure-blob", location=loc))
        except Exception:
            pass
        return refs

    async def health_check(self) -> bool:
        try:
            client = await self._ensure_client()
            url = self._container_url()
            sep = "&" if "?" in url else "?"
            url += f"{sep}restype=container"
            async with client.get(url) as resp:
                return resp.status in (200, 404)
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        return {
            "used_bytes": None,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": None,
        }

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
