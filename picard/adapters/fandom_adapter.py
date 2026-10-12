"""Fandom/Wikia adapter — stores chunks on Fandom wikis.

Chunks are stored as user subpage content via the MediaWiki API.

Notice: This adapter must only be used with Fandom wiki accounts and
wikis you own or have explicit written authorization to modify.  Use
in a manner that violates Fandom's Terms of Service is outside the
scope of this project and the responsibility of the user.  No
endorsement by or affiliation with Fandom, Inc. is implied.

Requires: pip install picard-store[wiki]  (aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

import aiohttp

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class FandomAdapter(Adapter):
    """Scatter chunks into Fandom/Wikia wiki pages."""

    def __init__(
        self,
        wiki_domain: str,
        username: str,
        password: str,
        *,
        page_prefix: str | None = None,
        max_chunk_bytes: int = 65536,
    ) -> None:
        self._wiki_domain = wiki_domain.rstrip("/")
        self._api_url = f"https://{self._wiki_domain}/api.php"
        self._username = username
        self._password = password
        self._page_prefix = page_prefix or f"User:{username}/"
        self._max = max_chunk_bytes
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
            name="fandom",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=8,
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
                "summary": "update",
                "token": token,
                "format": "json",
            },
        )
        resp.raise_for_status()

        loc = f"fandom://{self._wiki_domain}/{page_title}"
        self._cache[loc] = encoded_chunk
        return StoredChunkRef(adapter_name="fandom", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]

        parts = ref.location.replace("fandom://", "").split("/", 1)
        page_title = parts[1] if len(parts) > 1 else ref.location

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
        parts = ref.location.replace("fandom://", "").split("/", 1)
        page_title = parts[1] if len(parts) > 1 else ref.location

        try:
            session = await self._get_session()
            token = await self._login(session)
            resp = await session.post(
                self._api_url,
                data={
                    "action": "edit",
                    "title": page_title,
                    "text": "",
                    "summary": "cleanup",
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
            StoredChunkRef(adapter_name="fandom", location=loc)
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
