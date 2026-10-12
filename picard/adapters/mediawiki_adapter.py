"""MediaWiki adapter — stores chunks as wiki page edits.

Each chunk becomes the content of a user subpage.

WARNING: Wiki revision history is permanent and admin-visible. Deleted
pages remain in the revision log; do not store sensitive data on wikis
you do not fully control.

Requires: pip install picard[wiki]  (aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

import aiohttp

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class MediaWikiAdapter(Adapter):
    """Scatter chunks into MediaWiki page edits."""

    def __init__(
        self,
        api_url: str,
        username: str,
        password: str,
        *,
        page_prefix: str | None = None,
        max_chunk_bytes: int = 65536,
        max_posts: int = 20,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._username = username
        self._password = password
        self._page_prefix = page_prefix or f"User:{username}/picard/"
        self._max = max_chunk_bytes
        self._max_posts = max_posts
        self._post_count = 0
        self._token: str | None = None
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
            name="mediawiki",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=10,
        )

    async def _login(self, session: aiohttp.ClientSession) -> str:
        if self._token:
            return self._token

        resp = await session.get(
            self._api_url,
            params={"action": "query", "meta": "tokens", "type": "login", "format": "json"},
        )
        data = await resp.json()
        login_token = data["query"]["tokens"]["logintoken"]

        await session.post(
            self._api_url,
            data={
                "action": "login",
                "lgname": self._username,
                "lgpassword": self._password,
                "lgtoken": login_token,
                "format": "json",
            },
        )

        resp = await session.get(
            self._api_url,
            params={"action": "query", "meta": "tokens", "format": "json"},
        )
        data = await resp.json()
        self._token = data["query"]["tokens"]["csrftoken"]
        return self._token

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        if self._post_count >= self._max_posts:
            raise RuntimeError(
                f"MediaWiki session post cap reached ({self._max_posts}); "
                "raise max_posts or use a different wiki"
            )
        try:
            chunk = Chunk.decode(encoded_chunk)
            key = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
        except ValueError:
            key = uuid.uuid4().hex

        page_title = f"{self._page_prefix}{key}"

        session = await self._get_session()
        token = await self._login(session)
        resp = await session.post(
            self._api_url,
            data={
                "action": "edit",
                "title": page_title,
                "text": encoded_chunk,
                "summary": "picard chunk",
                "token": token,
                "format": "json",
            },
        )
        resp.raise_for_status()

        loc = f"wiki://{self._api_url}/{page_title}"
        self._cache[loc] = encoded_chunk
        self._post_count += 1
        return StoredChunkRef(adapter_name="mediawiki", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]

        page_title = ref.location.split("/", 3)[-1] if "/" in ref.location else ref.location

        session = await self._get_session()
        resp = await session.get(
            self._api_url,
            params={
                "action": "parse",
                "page": page_title,
                "prop": "wikitext",
                "format": "json",
            },
        )
        resp.raise_for_status()
        data = await resp.json()

        return data["parse"]["wikitext"]["*"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        self._cache.pop(ref.location, None)
        page_title = ref.location.split("/", 3)[-1] if "/" in ref.location else ref.location

        try:
            session = await self._get_session()
            token = await self._login(session)
            resp = await session.post(
                self._api_url,
                data={
                    "action": "edit",
                    "title": page_title,
                    "text": "",
                    "summary": "picard chunk removed",
                    "token": token,
                    "format": "json",
                },
            )
            resp.raise_for_status()
            return True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="mediawiki", location=loc)
            for loc in self._cache
            if manifest_id in loc
        ]

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            resp = await session.get(
                self._api_url,
                params={"action": "query", "meta": "siteinfo", "format": "json"},
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
