"""Pinterest adapter — stores chunks in pin descriptions.

Creates pins with chunk data in the description field, pointed at
a placeholder image URL.

Requires: pip install picard[pinterest]   (uses aiohttp)
"""

from __future__ import annotations

from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_API_BASE = "https://api.pinterest.com/v5"


class PinterestAdapter(Adapter):
    """Scatter chunks into Pinterest pin descriptions."""

    def __init__(
        self,
        access_token: str,
        board_id: str,
        *,
        max_chunk_bytes: int = 500,
    ) -> None:
        self._access_token = access_token
        self._board_id = board_id
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="pinterest",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_API_BASE}/pins",
                headers=self._headers(),
                json={
                    "board_id": self._board_id,
                    "description": encoded_chunk,
                    "title": "pchk",
                    "media_source": {
                        "source_type": "image_url",
                        "url": "https://via.placeholder.com/1",
                    },
                },
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        loc = f"pinterest://{self._board_id}/{data['id']}"
        return StoredChunkRef(adapter_name="pinterest", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import aiohttp

        pin_id = ref.location.rsplit("/", 1)[-1]
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{_API_BASE}/pins/{pin_id}",
                headers=self._headers(),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        return data.get("description", "")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        import aiohttp

        pin_id = ref.location.rsplit("/", 1)[-1]
        try:
            async with aiohttp.ClientSession() as session:
                async with session.delete(
                    f"{_API_BASE}/pins/{pin_id}",
                    headers=self._headers(),
                ) as resp:
                    return resp.status in (200, 204)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        import aiohttp

        refs: list[StoredChunkRef] = []
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{_API_BASE}/boards/{self._board_id}/pins",
                headers=self._headers(),
                params={"page_size": 100},
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        for pin in data.get("items", []):
            desc = pin.get("description", "")
            if manifest_id in desc:
                refs.append(
                    StoredChunkRef(
                        adapter_name="pinterest",
                        location=f"pinterest://{self._board_id}/{pin['id']}",
                    )
                )
        return refs

    async def health_check(self) -> bool:
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{_API_BASE}/user_account",
                    headers=self._headers(),
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False
