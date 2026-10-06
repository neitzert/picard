"""YouTube comments adapter — stores chunks as video comments.

Each chunk becomes a comment on a YouTube video:
    yt://{video_id}/{comment_id}

Requires: pip install picard[youtube]
"""

from __future__ import annotations

from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_MARKER = "PCHK:"


class YouTubeCommentsAdapter(Adapter):
    """Scatter chunks into YouTube video comments."""

    def __init__(
        self,
        video_id: str,
        *,
        oauth_token: str,
        api_key: str | None = None,
        max_chunk_bytes: int = 8000,
    ) -> None:
        self._video_id = video_id
        self._oauth_token = oauth_token
        self._api_key = api_key
        self._max = max_chunk_bytes
        self._base = "https://www.googleapis.com/youtube/v3"

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="youtube-comments",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._oauth_token}",
            "Content-Type": "application/json",
        }

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import aiohttp

        body = {
            "snippet": {
                "videoId": self._video_id,
                "topLevelComment": {
                    "snippet": {
                        "textOriginal": f"{_MARKER}{encoded_chunk}",
                    }
                },
            }
        }

        async with aiohttp.ClientSession() as session:
            url = f"{self._base}/commentThreads?part=snippet"
            async with session.post(
                url, json=body, headers=self._headers()
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                comment_id = data["id"]

        location = f"yt://{self._video_id}/{comment_id}"
        return StoredChunkRef(adapter_name="youtube-comments", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import aiohttp

        comment_id = ref.location.rsplit("/", 1)[1]
        params = {"part": "snippet", "id": comment_id}
        if self._api_key:
            params["key"] = self._api_key

        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._base}/comments", params=params, headers=self._headers()
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

        items = data.get("items", [])
        if not items:
            raise KeyError(f"comment not found: {comment_id}")
        text = items[0]["snippet"]["textOriginal"]
        if text.startswith(_MARKER):
            text = text[len(_MARKER) :]
        return text

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        import aiohttp

        comment_id = ref.location.rsplit("/", 1)[1]

        try:
            async with aiohttp.ClientSession() as session:
                async with session.delete(
                    f"{self._base}/comments",
                    params={"id": comment_id},
                    headers=self._headers(),
                ) as resp:
                    return resp.status in (200, 204)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        import aiohttp

        refs: list[StoredChunkRef] = []
        params = {
            "part": "snippet",
            "videoId": self._video_id,
            "maxResults": "100",
            "searchTerms": f"{_MARKER}",
        }
        if self._api_key:
            params["key"] = self._api_key

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._base}/commentThreads",
                    params=params,
                    headers=self._headers(),
                ) as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json()
                    for item in data.get("items", []):
                        text = item["snippet"]["topLevelComment"]["snippet"][
                            "textOriginal"
                        ]
                        if _MARKER in text and manifest_id in text:
                            refs.append(
                                StoredChunkRef(
                                    adapter_name="youtube-comments",
                                    location=f"yt://{self._video_id}/{item['id']}",
                                )
                            )
        except Exception:
            pass
        return refs

    async def health_check(self) -> bool:
        import aiohttp

        try:
            params = {
                "part": "snippet",
                "videoId": self._video_id,
                "maxResults": "1",
            }
            if self._api_key:
                params["key"] = self._api_key
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._base}/commentThreads",
                    params=params,
                    headers=self._headers(),
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False
