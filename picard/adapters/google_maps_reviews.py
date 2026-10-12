"""Google Maps Reviews adapter — stores chunks in place review data.

Writes chunk data via the Google Places API to places you own or
manage (Google Business Profile).

WARNING: Automated review submission may violate Google's ToS and
consumer-protection law regardless of ownership. Use only with Google
Business Profile test environments or private place IDs.

Requires: pip install picard[google-maps]  (aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

import aiohttp

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class GoogleMapsAdapter(Adapter):
    """Scatter chunks into Google Maps place reviews."""

    def __init__(
        self,
        api_key: str,
        place_id: str,
        *,
        max_chunk_bytes: int = 4096,
        rating: int = 5,
    ) -> None:
        self._api_key = api_key
        self._place_id = place_id
        self._max = max_chunk_bytes
        self._rating = rating
        self._cache: dict[str, str] = {}
        self._session = None


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
            name="google-maps",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=10,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            key = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
        except ValueError:
            key = uuid.uuid4().hex

        review_text = f"PICARD:{key}:{encoded_chunk}"
        session = await self._get_session()
        resp = await session.post(
            "https://maps.googleapis.com/maps/api/place/reviews/submit",
            params={"key": self._api_key},
            json={
                "place_id": self._place_id,
                "text": review_text,
                "rating": self._rating,
            },
        )
        resp.raise_for_status()
        data = await resp.json()

        review_id = data.get("review_id", key)
        loc = f"gmaps://{self._place_id}/{review_id}"
        self._cache[loc] = encoded_chunk
        return StoredChunkRef(adapter_name="google-maps", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]

        session = await self._get_session()
        resp = await session.get(
            "https://maps.googleapis.com/maps/api/place/details/json",
            params={
                "key": self._api_key,
                "place_id": self._place_id,
                "fields": "reviews",
            },
        )
        resp.raise_for_status()
        data = await resp.json()

        review_id = ref.location.split("/")[-1]
        for review in data.get("result", {}).get("reviews", []):
            text = review.get("text", "")
            if f"PICARD:" in text and review_id in text:
                return text.split(":", 2)[-1]

        raise KeyError(f"review not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="google-maps", location=loc)
            for loc in self._cache
            if manifest_id in loc
        ]

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            resp = await session.get(
                "https://maps.googleapis.com/maps/api/place/details/json",
                params={
                    "key": self._api_key,
                    "place_id": self._place_id,
                    "fields": "name",
                },
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
