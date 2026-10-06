"""WordPress adapter — stores chunks as draft posts via WP REST API.

Posts are created with status="draft" so they're invisible to the public.
Any self-hosted WordPress site with the REST API enabled works.

Requires: pip install picard[wordpress]   (uses aiohttp)
"""

from __future__ import annotations

import base64
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class WordPressAdapter(Adapter):
    """Scatter chunks into WordPress draft posts."""

    def __init__(
        self,
        site_url: str,
        username: str,
        application_password: str,
        *,
        use_drafts: bool = True,
        max_chunk_bytes: int = 65536,
    ) -> None:
        self._site_url = site_url.rstrip("/")
        self._username = username
        self._app_password = application_password
        self._use_drafts = use_drafts
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="wordpress",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=60,
        )

    def _headers(self) -> dict[str, str]:
        cred = base64.b64encode(
            f"{self._username}:{self._app_password}".encode()
        ).decode()
        return {
            "Authorization": f"Basic {cred}",
            "Content-Type": "application/json",
        }

    def _api_url(self, path: str = "") -> str:
        return f"{self._site_url}/wp-json/wp/v2{path}"

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.post(
                self._api_url("/posts"),
                headers=self._headers(),
                json={
                    "title": "pchk",
                    "content": encoded_chunk,
                    "status": "draft" if self._use_drafts else "private",
                },
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        loc = f"wp://{self._site_url}/{data['id']}"
        return StoredChunkRef(adapter_name="wordpress", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import aiohttp

        post_id = ref.location.rsplit("/", 1)[-1]
        async with aiohttp.ClientSession() as session:
            async with session.get(
                self._api_url(f"/posts/{post_id}"),
                headers=self._headers(),
                params={"context": "edit"},
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        raw = data.get("content", {}).get("raw", "")
        return raw if raw else data.get("content", {}).get("rendered", "")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        import aiohttp

        post_id = ref.location.rsplit("/", 1)[-1]
        try:
            async with aiohttp.ClientSession() as session:
                async with session.delete(
                    self._api_url(f"/posts/{post_id}"),
                    headers=self._headers(),
                    params={"force": "true"},
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        import aiohttp

        refs: list[StoredChunkRef] = []
        page = 1
        async with aiohttp.ClientSession() as session:
            while True:
                async with session.get(
                    self._api_url("/posts"),
                    headers=self._headers(),
                    params={
                        "status": "draft,private",
                        "per_page": 100,
                        "page": page,
                        "context": "edit",
                    },
                ) as resp:
                    if resp.status != 200:
                        break
                    posts = await resp.json()
                if not posts:
                    break
                for post in posts:
                    raw = post.get("content", {}).get("raw", "")
                    if manifest_id in raw:
                        refs.append(
                            StoredChunkRef(
                                adapter_name="wordpress",
                                location=f"wp://{self._site_url}/{post['id']}",
                            )
                        )
                if len(posts) < 100:
                    break
                page += 1
        return refs

    async def health_check(self) -> bool:
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self._api_url("/users/me"),
                    headers=self._headers(),
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False
