"""0x0.st adapter — stores chunks as uploaded files. No account needed.

Each chunk becomes a hosted file:
    0x0://{file_url}

Zero signup — just upload.
"""

from __future__ import annotations

from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class NullPointerAdapter(Adapter):
    """Scatter chunks into 0x0.st file uploads — zero signup required."""

    def __init__(
        self,
        *,
        url: str = "https://0x0.st",
        max_chunk_bytes: int = 536_870_912,
    ) -> None:
        self._url = url.rstrip("/")
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="0x0",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=10,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import aiohttp

        form = aiohttp.FormData()
        form.add_field(
            "file",
            encoded_chunk.encode(),
            filename="chunk.pchk",
            content_type="application/octet-stream",
        )

        async with aiohttp.ClientSession() as session:
            async with session.post(self._url, data=form) as resp:
                resp.raise_for_status()
                file_url = (await resp.text()).strip()

        return StoredChunkRef(adapter_name="0x0", location=f"0x0://{file_url}")

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import aiohttp

        file_url = ref.location.split("://", 1)[1]

        async with aiohttp.ClientSession() as session:
            async with session.get(file_url) as resp:
                resp.raise_for_status()
                return await resp.text()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.head(self._url) as resp:
                    return resp.status == 200
        except Exception:
            return False
