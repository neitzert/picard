"""Steam adapter — stores chunks as Steam profile comments or Workshop descriptions.

Uses the Steam Web API to post comments on a user's own profile or
create Workshop items with chunk data in the description field.

Requires: pip install picard[steam]  (aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class SteamAdapter(Adapter):
    """Scatter chunks into Steam profile comments."""

    def __init__(
        self,
        api_key: str,
        steam_id: str,
        *,
        session_id: str | None = None,
        login_cookie: str | None = None,
        max_chunk_bytes: int = 8000,
    ) -> None:
        self._api_key = api_key
        self._steam_id = steam_id
        self._session_id = session_id
        self._login_cookie = login_cookie
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="steam",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=15,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import aiohttp

        try:
            from picard.chunker import Chunk
            chunk = Chunk.decode(encoded_chunk)
            chunk_id = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
        except (ValueError, Exception):
            chunk_id = uuid.uuid4().hex

        cookies = {}
        if self._login_cookie:
            cookies["steamLoginSecure"] = self._login_cookie
        if self._session_id:
            cookies["sessionid"] = self._session_id

        async with aiohttp.ClientSession(cookies=cookies) as session:
            async with session.post(
                f"https://steamcommunity.com/comment/Profile/post/{self._steam_id}/-1/",
                data={
                    "comment": encoded_chunk,
                    "sessionid": self._session_id or "",
                },
            ) as resp:
                data = await resp.json()
                comment_id = str(data.get("gidcomment", chunk_id))

        location = f"steam://{self._steam_id}/{comment_id}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="steam", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]

        import aiohttp

        parts = ref.location.removeprefix("steam://").split("/")
        steam_id = parts[0]

        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://steamcommunity.com/comment/Profile/render/{steam_id}/-1/",
            ) as resp:
                data = await resp.json()
                html = data.get("comments_html", "")
                raise KeyError(f"chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="steam", location=loc)
            for loc, data in self._cache.items()
            if manifest_id in loc or manifest_id in data
        ]

    async def health_check(self) -> bool:
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/",
                    params={"key": self._api_key, "steamids": self._steam_id},
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        used = sum(len(v.encode()) for v in self._cache.values())
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._cache),
        }
