"""OSPF adapter — store chunks in OSPF Opaque LSAs (RFC 5250).

Injects Opaque LSAs with chunk data into the OSPF LSDB via a router
management API (FRRouting REST, BIRD, or Quagga).

Data exfil insight: Opaque LSAs flood across the entire OSPF area.
Every router in the area stores a copy of your chunk in its LSDB.

Requires: pip install picard[ospf]  (aiohttp)
"""

from __future__ import annotations

import base64
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class OspfAdapter(Adapter):
    """Scatter chunks into OSPF Opaque LSAs."""

    def __init__(
        self,
        router_api_url: str,
        *,
        area_id: str = "0.0.0.0",
        opaque_type: int = 128,
        max_chunk_bytes: int = 1024,
    ) -> None:
        self._api_url = router_api_url.rstrip("/")
        self._area_id = area_id
        self._opaque_type = opaque_type
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
            name="ospf",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=10,
        )

    def _chunk_key(self, encoded_chunk: str) -> str:
        from picard.chunker import Chunk
        try:
            chunk = Chunk.decode(encoded_chunk)
            return f"{chunk.manifest_id}_{chunk.block_index}_{chunk.chunk_index}"
        except (ValueError, Exception):
            return uuid.uuid4().hex

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        key = self._chunk_key(encoded_chunk)
        opaque_id = hash(key) & 0xFFFFFF
        data_b64 = base64.b64encode(encoded_chunk.encode()).decode()

        payload = {
            "area": self._area_id,
            "opaque_type": self._opaque_type,
            "opaque_id": opaque_id,
            "data": data_b64,
        }

        session = await self._get_session()
        async with session.post(
            f"{self._api_url}/ospf/opaque-lsa", json=payload
        ) as resp:
            resp.raise_for_status()

        loc = f"ospf://{self._area_id}/{self._opaque_type}/{opaque_id}"
        return StoredChunkRef(adapter_name="ospf", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        parts = ref.location.replace("ospf://", "").split("/")
        area = parts[0]
        opaque_type = int(parts[1])
        opaque_id = int(parts[2])

        session = await self._get_session()
        async with session.get(
            f"{self._api_url}/ospf/opaque-lsa",
            params={
                "area": area,
                "opaque_type": opaque_type,
                "opaque_id": opaque_id,
            },
        ) as resp:
            resp.raise_for_status()
            result = await resp.json()

        return base64.b64decode(result["data"]).decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        parts = ref.location.replace("ospf://", "").split("/")
        area = parts[0]
        opaque_type = int(parts[1])
        opaque_id = int(parts[2])

        try:
            session = await self._get_session()
            async with session.delete(
                f"{self._api_url}/ospf/opaque-lsa",
                params={
                    "area": area,
                    "opaque_type": opaque_type,
                    "opaque_id": opaque_id,
                },
            ) as resp:
                return resp.status in (200, 204)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        try:
            session = await self._get_session()
            async with session.get(
                f"{self._api_url}/ospf/opaque-lsa",
                params={"area": self._area_id, "opaque_type": self._opaque_type},
            ) as resp:
                resp.raise_for_status()
                result = await resp.json()

            return [
                StoredChunkRef(
                    adapter_name="ospf",
                    location=f"ospf://{self._area_id}/{self._opaque_type}/{lsa['opaque_id']}",
                )
                for lsa in result.get("lsas", [])
            ]
        except Exception:
            return []

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get(
                f"{self._api_url}/ospf/neighbors"
            ) as resp:
                return resp.status == 200
        except Exception:
            return False
