"""ix.io adapter — stores chunks as ix.io pastes. No account needed.

Each chunk becomes a paste:
    ix://{paste_id}
"""

from __future__ import annotations

from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class IxAdapter(Adapter):
    """Scatter chunks into ix.io pastes — zero signup required."""

    def __init__(
        self,
        *,
        url: str = "http://ix.io",
        max_chunk_bytes: int = 524_288,
    ) -> None:
        self._url = url.rstrip("/")
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
            name="ix-io",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=10,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        data = {"f:1": encoded_chunk}

        session = await self._get_session()
        async with session.post(self._url, data=data) as resp:
            resp.raise_for_status()
            url = (await resp.text()).strip()

        paste_id = url.rstrip("/").rsplit("/", 1)[-1]
        return StoredChunkRef(adapter_name="ix-io", location=f"ix://{paste_id}")

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        paste_id = ref.location.split("://", 1)[1]

        session = await self._get_session()
        async with session.get(f"{self._url}/{paste_id}") as resp:
            resp.raise_for_status()
            return await resp.text()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get(self._url) as resp:
                return resp.status == 200
        except Exception:
            return False
