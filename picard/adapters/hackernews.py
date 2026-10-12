"""Hacker News adapter — stores chunks as HN comments.

Write via form POST, read via Firebase API:
    hn://{parent_item_id}/{comment_id}

Notice: This adapter must only be used with a Hacker News account
you own and control, and only in ways consistent with HN's guidelines.
Bulk or automated posting violates HN's rules and is not a supported
use case.  No endorsement by or affiliation with Y Combinator is
implied.

Requires: pip install picard[hn]
"""

from __future__ import annotations

from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_MARKER = "[PCHK]"
_HN_API = "https://hacker-news.firebaseio.com"
_HN_WEB = "https://news.ycombinator.com"


class HackerNewsAdapter(Adapter):
    """Scatter chunks into Hacker News comments."""

    def __init__(
        self,
        username: str,
        password: str,
        parent_item_id: int,
        *,
        max_chunk_bytes: int = 4096,
        max_posts: int = 5,
    ) -> None:
        self._username = username
        self._password = password
        self._parent_id = parent_item_id
        self._max = max_chunk_bytes
        self._max_posts = max_posts
        self._post_count = 0
        self._cookie: str | None = None
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
            name="hackernews",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=1,
        )

    async def _login(self, session) -> None:
        if self._cookie:
            return
        data = {
            "acct": self._username,
            "pw": self._password,
            "goto": "news",
        }
        async with session.post(f"{_HN_WEB}/login", data=data) as resp:
            cookies = resp.cookies
            if "user" in cookies:
                self._cookie = cookies["user"].value

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        if self._post_count >= self._max_posts:
            raise RuntimeError(
                f"HackerNews adapter reached max_posts={self._max_posts} per session"
            )
        text = f"{_MARKER}{encoded_chunk}"

        session = await self._get_session()
        await self._login(session)
        headers = {"Cookie": f"user={self._cookie}"} if self._cookie else {}

        data = {
            "parent": str(self._parent_id),
            "text": text,
            "goto": f"item?id={self._parent_id}",
        }
        async with session.post(
            f"{_HN_WEB}/comment", data=data, headers=headers
        ) as resp:
            resp.raise_for_status()

        # Fetch parent to find our comment
        async with session.get(
            f"{_HN_API}/v0/item/{self._parent_id}.json"
        ) as resp:
            parent_data = await resp.json()
            kids = parent_data.get("kids", [])
            comment_id = kids[-1] if kids else 0

        self._post_count += 1
        location = f"hn://{self._parent_id}/{comment_id}"
        return StoredChunkRef(adapter_name="hackernews", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        comment_id = ref.location.rsplit("/", 1)[1]

        session = await self._get_session()
        async with session.get(
            f"{_HN_API}/v0/item/{comment_id}.json"
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        text = data.get("text", "")
        if text.startswith(_MARKER):
            text = text[len(_MARKER) :]
        return text

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        try:
            session = await self._get_session()
            async with session.get(
                f"{_HN_API}/v0/item/{self._parent_id}.json"
            ) as resp:
                data = await resp.json()
                for kid_id in data.get("kids", []):
                    async with session.get(
                        f"{_HN_API}/v0/item/{kid_id}.json"
                    ) as kresp:
                        kdata = await kresp.json()
                        text = kdata.get("text", "")
                        if _MARKER in text and manifest_id in text:
                            refs.append(
                                StoredChunkRef(
                                    adapter_name="hackernews",
                                    location=f"hn://{self._parent_id}/{kid_id}",
                                )
                            )
        except Exception:
            pass
        return refs

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get(
                f"{_HN_API}/v0/item/{self._parent_id}.json"
            ) as resp:
                return resp.status == 200
        except Exception:
            return False
