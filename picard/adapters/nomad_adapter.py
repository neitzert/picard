"""HashiCorp Nomad adapter — stores chunks as Nomad Variables.

Hides data in the cluster's variable store alongside legitimate
application configuration.

Requires: pip install picard[nomad]  (aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class NomadAdapter(Adapter):
    """Scatter chunks into HashiCorp Nomad Variables."""

    def __init__(
        self,
        *,
        address: str = "http://localhost:4646",
        token: str | None = None,
        namespace: str = "default",
        path_prefix: str = "picard",
        max_chunk_bytes: int = 65_536,
    ) -> None:
        self._address = address.rstrip("/")
        self._token = token
        self._namespace = namespace
        self._prefix = path_prefix
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
            name="nomad",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=200,
        )

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self._token:
            h["X-Nomad-Token"] = self._token
        return h

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            key = f"{chunk.manifest_id}-b{chunk.block_index}-c{chunk.chunk_index}"
        except ValueError:
            key = uuid.uuid4().hex

        path = f"{self._prefix}/{key}"
        url = f"{self._address}/v1/var/{path}"
        payload = {
            "Namespace": self._namespace,
            "Path": path,
            "Items": {"data": encoded_chunk},
        }

        session = await self._get_session()
        async with session.put(
            url, json=payload, headers=self._headers()
        ) as resp:
            resp.raise_for_status()

        location = f"nomad://{self._namespace}/var/{path}"
        return StoredChunkRef(adapter_name="nomad", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        path = ref.location.split("/var/", 1)[-1]
        url = f"{self._address}/v1/var/{path}"

        session = await self._get_session()
        async with session.get(url, headers=self._headers()) as resp:
            resp.raise_for_status()
            body = await resp.json()

        return body["Items"]["data"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        path = ref.location.split("/var/", 1)[-1]
        url = f"{self._address}/v1/var/{path}"

        try:
            session = await self._get_session()
            async with session.delete(url, headers=self._headers()) as resp:
                return resp.status in (200, 204)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        url = f"{self._address}/v1/vars"
        params = {"prefix": f"{self._prefix}/{manifest_id}"}

        session = await self._get_session()
        async with session.get(
            url, params=params, headers=self._headers()
        ) as resp:
            resp.raise_for_status()
            items = await resp.json()

        return [
            StoredChunkRef(
                adapter_name="nomad",
                location=f"nomad://{self._namespace}/var/{v['Path']}",
            )
            for v in (items or [])
        ]

    async def health_check(self) -> bool:
        url = f"{self._address}/v1/status/leader"
        try:
            session = await self._get_session()
            async with session.get(url, headers=self._headers()) as resp:
                return resp.status == 200
        except Exception:
            return False
