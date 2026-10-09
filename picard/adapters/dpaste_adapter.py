"""dpaste adapter — stores chunks as dpaste.org pastes. No account needed.

Each chunk becomes a paste:
    dpaste://{slug}
"""

from __future__ import annotations

from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class DpasteAdapter(Adapter):
    """Scatter chunks into dpaste.org pastes — zero signup required."""

    def __init__(
        self,
        *,
        api_url: str = "https://dpaste.org/api/",
        expiry_days: int = 365,
        max_chunk_bytes: int = 250_000,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._expiry_days = expiry_days
        self._max = max_chunk_bytes
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
            name="dpaste",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=10,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        data = {
            "content": encoded_chunk,
            "format": "url",
            "expires": str(self._expiry_days),
        }

        session = await self._get_session()
        async with session.post(self._api_url, data=data) as resp:
            resp.raise_for_status()
            url = (await resp.text()).strip()

        slug = url.rstrip("/").rsplit("/", 1)[-1]
        return StoredChunkRef(adapter_name="dpaste", location=f"dpaste://{slug}")

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        slug = ref.location.split("://", 1)[1]
        base = self._api_url.rsplit("/api", 1)[0]

        session = await self._get_session()
        async with session.get(f"{base}/{slug}.txt") as resp:
            resp.raise_for_status()
            return await resp.text()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get(
                self._api_url.rsplit("/api", 1)[0]
            ) as resp:
                return resp.status == 200
        except Exception:
            return False
