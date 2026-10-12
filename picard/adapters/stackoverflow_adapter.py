"""StackExchange adapter — stores chunks as answers on an SE site.

Each chunk becomes an answer on a StackExchange question:
    so://{site}/{answer_id}

WARNING: Use only with a private/self-hosted StackExchange instance
or a dedicated test account on a site where you have permission to
post programmatically.  Automated posting on public sites (including
stackoverflow.com) violates the StackExchange ToS.

Requires: pip install picard-store[stackoverflow]
"""

from __future__ import annotations

from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_API = "https://api.stackexchange.com/2.3"
_MARKER = "<!-- PCHK -->"


class StackOverflowAdapter(Adapter):
    """Scatter chunks into StackExchange answers."""

    def __init__(
        self,
        access_token: str,
        question_id: int,
        *,
        api_key: str | None = None,
        site: str = "stackoverflow",
        max_chunk_bytes: int = 30_000,
        max_posts: int = 5,
    ) -> None:
        self._token = access_token
        self._question_id = question_id
        self._api_key = api_key
        self._site = site
        self._max = max_chunk_bytes
        self._session = None
        self._max_posts = max_posts
        self._post_count = 0


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
            name="stackoverflow",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=10,
        )

    def _params(self) -> dict:
        p: dict = {"site": self._site, "access_token": self._token}
        if self._api_key:
            p["key"] = self._api_key
        return p

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        if self._post_count >= self._max_posts:
            raise RuntimeError(
                f"stackoverflow adapter reached max_posts={self._max_posts} per session"
            )
        body = f"{_MARKER}\n{encoded_chunk}"
        params = self._params()
        data = {"body": body}

        session = await self._get_session()
        async with session.post(
            f"{_API}/questions/{self._question_id}/answers",
            params=params,
            data=data,
        ) as resp:
            resp.raise_for_status()
            result = await resp.json()

        answer_id = result["items"][0]["answer_id"]
        location = f"so://{self._site}/{answer_id}"
        self._post_count += 1
        return StoredChunkRef(adapter_name="stackoverflow", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        parts = ref.location.split("://", 1)[1].split("/")
        site = parts[0]
        answer_id = parts[1]
        params = self._params()
        params["filter"] = "withbody"
        params["site"] = site

        session = await self._get_session()
        async with session.get(
            f"{_API}/answers/{answer_id}", params=params
        ) as resp:
            resp.raise_for_status()
            result = await resp.json()

        items = result.get("items", [])
        if not items:
            raise KeyError(f"answer not found: {answer_id}")
        body = items[0]["body"]
        if _MARKER in body:
            body = body.split(_MARKER, 1)[1].strip()
        return body

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        parts = ref.location.split("://", 1)[1].split("/")
        answer_id = parts[1]
        params = self._params()

        try:
            session = await self._get_session()
            async with session.delete(
                f"{_API}/answers/{answer_id}", params=params
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        params = self._params()
        params["filter"] = "withbody"
        params["sort"] = "creation"
        params["order"] = "asc"

        try:
            session = await self._get_session()
            async with session.get(
                f"{_API}/questions/{self._question_id}/answers",
                params=params,
            ) as resp:
                if resp.status != 200:
                    return []
                result = await resp.json()
                for item in result.get("items", []):
                    body = item.get("body", "")
                    if _MARKER in body and manifest_id in body:
                        refs.append(
                            StoredChunkRef(
                                adapter_name="stackoverflow",
                                location=f"so://{self._site}/{item['answer_id']}",
                            )
                        )
        except Exception:
            pass
        return refs

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get(
                f"{_API}/info", params={"site": self._site}
            ) as resp:
                return resp.status == 200
        except Exception:
            return False
