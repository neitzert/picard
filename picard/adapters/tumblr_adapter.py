"""Tumblr adapter — stores chunks as draft text posts.

Posts are created with state="draft" so they don't appear publicly.
The blog owner can see and manage them from the dashboard.

Requires: pip install picard[tumblr]   (uses aiohttp)
"""

from __future__ import annotations

from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_API_BASE = "https://api.tumblr.com/v2"


class TumblrAdapter(Adapter):
    """Scatter chunks into Tumblr draft posts."""

    def __init__(
        self,
        consumer_key: str,
        consumer_secret: str,
        oauth_token: str,
        oauth_secret: str,
        blog_name: str,
        *,
        max_chunk_bytes: int = 65536,
    ) -> None:
        self._consumer_key = consumer_key
        self._consumer_secret = consumer_secret
        self._oauth_token = oauth_token
        self._oauth_secret = oauth_secret
        self._blog = blog_name
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="tumblr",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._oauth_token}",
            "Content-Type": "application/json",
        }

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_API_BASE}/blog/{self._blog}/post",
                headers=self._headers(),
                json={
                    "type": "text",
                    "title": "pchk",
                    "body": encoded_chunk,
                    "state": "draft",
                },
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                post_id = data.get("response", {}).get("id", "")
        loc = f"tumblr://{self._blog}/{post_id}"
        return StoredChunkRef(adapter_name="tumblr", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import aiohttp

        post_id = ref.location.rsplit("/", 1)[-1]
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{_API_BASE}/blog/{self._blog}/posts/{post_id}",
                headers=self._headers(),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        posts = data.get("response", {}).get("posts", [])
        if not posts:
            raise KeyError(f"post not found: {post_id}")
        return posts[0].get("body", "")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        import aiohttp

        post_id = ref.location.rsplit("/", 1)[-1]
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{_API_BASE}/blog/{self._blog}/post/delete",
                    headers=self._headers(),
                    json={"id": post_id},
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        import aiohttp

        refs: list[StoredChunkRef] = []
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{_API_BASE}/blog/{self._blog}/drafts",
                headers=self._headers(),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        for post in data.get("response", {}).get("posts", []):
            body = post.get("body", "")
            if manifest_id in body:
                refs.append(
                    StoredChunkRef(
                        adapter_name="tumblr",
                        location=f"tumblr://{self._blog}/{post['id']}",
                    )
                )
        return refs

    async def health_check(self) -> bool:
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{_API_BASE}/user/info",
                    headers=self._headers(),
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False
