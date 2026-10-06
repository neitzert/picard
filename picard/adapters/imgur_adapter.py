"""Imgur adapter — stores chunks in image descriptions.

Each chunk is uploaded as a 1x1 PNG with data in the description:
    imgur://{image_hash}

Requires: pip install picard[imgur]
"""

from __future__ import annotations

import base64
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_API = "https://api.imgur.com/3"
# 1x1 transparent PNG
_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
    "nGNgYPgPAAEDAQAIicLsAAAABJRU5ErkJggg=="
)


class ImgurAdapter(Adapter):
    """Scatter chunks into Imgur image descriptions."""

    def __init__(
        self,
        client_id: str,
        *,
        client_secret: str | None = None,
        access_token: str | None = None,
        max_chunk_bytes: int = 10_000,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._access_token = access_token
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="imgur",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    def _headers(self) -> dict:
        if self._access_token:
            return {"Authorization": f"Bearer {self._access_token}"}
        return {"Authorization": f"Client-ID {self._client_id}"}

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import aiohttp

        data = aiohttp.FormData()
        data.add_field("image", base64.b64encode(_PIXEL_PNG).decode())
        data.add_field("type", "base64")
        data.add_field("description", encoded_chunk)
        data.add_field("title", "pchk")

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_API}/image", data=data, headers=self._headers()
            ) as resp:
                resp.raise_for_status()
                result = await resp.json()

        image_hash = result["data"]["id"]
        location = f"imgur://{image_hash}"
        return StoredChunkRef(adapter_name="imgur", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import aiohttp

        image_hash = ref.location.split("://", 1)[1]

        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{_API}/image/{image_hash}", headers=self._headers()
            ) as resp:
                resp.raise_for_status()
                result = await resp.json()

        return result["data"]["description"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        import aiohttp

        image_hash = ref.location.split("://", 1)[1]

        try:
            async with aiohttp.ClientSession() as session:
                async with session.delete(
                    f"{_API}/image/{image_hash}", headers=self._headers()
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{_API}/credits", headers=self._headers()
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{_API}/credits", headers=self._headers()
                ) as resp:
                    data = await resp.json()
                    remaining = data["data"].get("ClientRemaining", 0)
                    return {
                        "used_bytes": None,
                        "free_bytes": None,
                        "total_bytes": None,
                        "chunk_count": remaining,
                    }
        except Exception:
            return {
                "used_bytes": None,
                "free_bytes": None,
                "total_bytes": None,
                "chunk_count": None,
            }
