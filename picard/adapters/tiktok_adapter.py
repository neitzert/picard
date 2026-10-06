"""TikTok adapter — stores chunks in video descriptions.

Posts video metadata via the Content Posting API and reads back
descriptions via the video query endpoint.

Requires: pip install picard[tiktok]   (uses aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_API_BASE = "https://open.tiktokapis.com"


class TikTokAdapter(Adapter):
    """Scatter chunks into TikTok video descriptions."""

    def __init__(
        self,
        access_token: str,
        open_id: str,
        *,
        max_chunk_bytes: int = 2200,
    ) -> None:
        self._access_token = access_token
        self._open_id = open_id
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="tiktok",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=10,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_API_BASE}/v2/post/publish/",
                headers=self._headers(),
                json={
                    "post_info": {
                        "title": encoded_chunk,
                        "privacy_level": "SELF_ONLY",
                    },
                    "source_info": {
                        "source": "PULL_FROM_URL",
                        "video_url": "https://example.com/placeholder.mp4",
                    },
                },
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                publish_id = data.get("data", {}).get(
                    "publish_id", uuid.uuid4().hex
                )
        loc = f"tiktok://{self._open_id}/{publish_id}"
        self._cache[loc] = encoded_chunk
        return StoredChunkRef(adapter_name="tiktok", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        cached = self._cache.get(ref.location)
        if cached:
            return cached

        import aiohttp

        video_id = ref.location.rsplit("/", 1)[-1]
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_API_BASE}/v2/video/query/",
                headers=self._headers(),
                json={"filters": {"video_ids": [video_id]}},
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                videos = data.get("data", {}).get("videos", [])
                if not videos:
                    raise KeyError(f"video not found: {video_id}")
                return videos[0].get("title", "")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        for loc, data in self._cache.items():
            if manifest_id in data:
                refs.append(StoredChunkRef(adapter_name="tiktok", location=loc))
        return refs

    async def health_check(self) -> bool:
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{_API_BASE}/v2/user/info/",
                    headers=self._headers(),
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False
