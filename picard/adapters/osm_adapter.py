"""OpenStreetMap Notes adapter — stores chunks as map notes.

Each chunk becomes a note at a deterministic lat/lon derived from the chunk key.

WARNING: OpenStreetMap is shared community infrastructure.  Only use this
adapter with a private or self-hosted OSM instance, or ensure your usage
complies with the OSM API usage policy and terms of use.  Flooding the
public OSM API with data notes is a terms violation and harms the community.

Requires: pip install picard-store[osm]  (aiohttp)
"""

from __future__ import annotations

import hashlib
import struct
import uuid
from typing import Sequence

import aiohttp

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class OsmAdapter(Adapter):
    """Scatter chunks into OpenStreetMap notes."""

    def __init__(
        self,
        *,
        api_url: str = "https://api.openstreetmap.org",
        username: str | None = None,
        password: str | None = None,
        max_chunk_bytes: int = 4096,
        max_posts: int = 10,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._username = username
        self._password = password
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}
        self._session = None
        self._max_posts = max_posts
        self._post_count = 0


    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="osm-notes",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=10,
        )

    @staticmethod
    def _key_to_latlon(key: str) -> tuple[float, float]:
        h = hashlib.sha256(key.encode()).digest()
        lat_raw, lon_raw = struct.unpack(">ii", h[:8])
        lat = (lat_raw % 18000000) / 100000.0 - 90.0
        lon = (lon_raw % 36000000) / 100000.0 - 180.0
        return round(lat, 5), round(lon, 5)

    def _auth(self) -> aiohttp.BasicAuth | None:
        if self._username and self._password:
            return aiohttp.BasicAuth(self._username, self._password)
        return None

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        if self._post_count >= self._max_posts:
            raise RuntimeError(
                f"osm-notes adapter reached max_posts={self._max_posts} per session"
            )
        try:
            chunk = Chunk.decode(encoded_chunk)
            key = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
        except ValueError:
            key = uuid.uuid4().hex

        lat, lon = self._key_to_latlon(key)
        text = f"PICARD:{key}:{encoded_chunk}"

        session = await self._get_session()
        resp = await session.post(
            f"{self._api_url}/api/0.6/notes.json",
            params={"lat": lat, "lon": lon, "text": text},
            auth=self._auth(),
        )
        resp.raise_for_status()
        data = await resp.json()

        note_id = str(data["properties"]["id"])
        loc = f"osm://notes/{note_id}"
        self._cache[loc] = encoded_chunk
        self._post_count += 1
        return StoredChunkRef(adapter_name="osm-notes", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]

        note_id = ref.location.split("/")[-1]
        session = await self._get_session()
        resp = await session.get(
            f"{self._api_url}/api/0.6/notes/{note_id}.json",
        )
        resp.raise_for_status()
        data = await resp.json()

        text = data["properties"]["comments"][0]["text"]
        chunk_data = text.split(":", 2)[-1]
        return chunk_data

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        self._cache.pop(ref.location, None)
        note_id = ref.location.split("/")[-1]
        try:
            session = await self._get_session()
            resp = await session.post(
                f"{self._api_url}/api/0.6/notes/{note_id}/close.json",
                params={"text": "removed"},
                auth=self._auth(),
            )
            resp.raise_for_status()
            return True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="osm-notes", location=loc)
            for loc in self._cache
            if manifest_id in loc
        ]

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            resp = await session.get(
                f"{self._api_url}/api/0.6/capabilities",
            )
            return resp.status == 200
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        return {
            "used_bytes": sum(len(v.encode()) for v in self._cache.values()),
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._cache),
        }
