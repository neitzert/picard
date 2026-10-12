"""Google Cloud Functions adapter — stores chunks via serverless function invocations.

Chunks are written and read by calling an HTTP-triggered Cloud Function.
The function acts as a stateless front-end to whatever storage the
function author wires up (Firestore, GCS, /tmp).

Requires: pip install picard[gcp]  (aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class GcpFunctionsAdapter(Adapter):
    """Scatter chunks via Google Cloud Functions HTTP triggers."""

    def __init__(
        self,
        function_url: str,
        *,
        auth_token: str | None = None,
        max_chunk_bytes: int = 10_485_760,
    ) -> None:
        self._function_url = function_url.rstrip("/")
        self._auth_token = auth_token
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
            name="gcp-functions",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=1000,
        )

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self._auth_token:
            h["Authorization"] = f"Bearer {self._auth_token}"
        return h

    def _chunk_key(self, encoded_chunk: str) -> str:
        try:
            chunk = Chunk.decode(encoded_chunk)
            return f"{chunk.manifest_id}/b{chunk.block_index}_c{chunk.chunk_index}"
        except ValueError:
            return uuid.uuid4().hex

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        key = self._chunk_key(encoded_chunk)
        session = await self._get_session()
        async with session.post(
            self._function_url,
            json={"action": "write", "key": key, "data": encoded_chunk},
            headers=self._headers(),
        ) as resp:
            resp.raise_for_status()
        return StoredChunkRef(
            adapter_name="gcp-functions",
            location=f"gcf://{self._function_url}/{key}",
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        key = ref.location.rsplit("/", 1)[-1] if "://" in ref.location else ref.location
        session = await self._get_session()
        async with session.post(
            self._function_url,
            json={"action": "read", "key": key},
            headers=self._headers(),
        ) as resp:
            resp.raise_for_status()
            result = await resp.json()
            data = result.get("data")
            if data is None:
                raise KeyError(f"chunk not found: {key}")
            return data

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        key = ref.location.rsplit("/", 1)[-1] if "://" in ref.location else ref.location
        try:
            session = await self._get_session()
            async with session.post(
                self._function_url,
                json={"action": "delete", "key": key},
                headers=self._headers(),
            ) as resp:
                result = await resp.json()
                return result.get("deleted", False)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        session = await self._get_session()
        async with session.post(
            self._function_url,
            json={"action": "list", "prefix": manifest_id},
            headers=self._headers(),
        ) as resp:
            resp.raise_for_status()
            result = await resp.json()
        return [
            StoredChunkRef(
                adapter_name="gcp-functions",
                location=f"gcf://{self._function_url}/{k}",
            )
            for k in result.get("keys", [])
        ]

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get(
                self._function_url,
                params={"action": "health"},
                headers=self._headers(),
            ) as resp:
                return resp.status == 200
        except Exception:
            return False
