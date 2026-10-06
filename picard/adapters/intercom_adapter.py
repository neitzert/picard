"""Intercom adapter -- stores chunks as conversation notes.

Data is embedded inside Intercom admin notes on conversations,
appearing as internal customer support annotations.

Requires: pip install picard[intercom]  (uses aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_API_URL = "https://api.intercom.io"


class IntercomAdapter(Adapter):
    """Scatter chunks into Intercom conversation notes."""

    def __init__(
        self,
        access_token: str,
        admin_id: str,
        *,
        max_chunk_bytes: int = 4000,
    ) -> None:
        self._access_token = access_token
        self._admin_id = admin_id
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="intercom",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=60,
            ttl_seconds=None,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import aiohttp

        chunk_id = uuid.uuid4().hex[:12]
        # Create a contact, then start a conversation with the data
        payload = {
            "from": {"type": "user", "id": self._admin_id},
            "body": f"PICARD:{chunk_id}:{encoded_chunk}",
        }
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_API_URL}/conversations",
                json=payload,
                headers=headers,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

        convo_id = data.get("conversation_id", data.get("id", chunk_id))
        location = f"intercom://conversations/{convo_id}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="intercom", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]
        raise KeyError(f"chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="intercom", location=loc)
            for loc in self._cache
        ]

    async def health_check(self) -> bool:
        import aiohttp

        try:
            headers = {
                "Authorization": f"Bearer {self._access_token}",
                "Accept": "application/json",
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{_API_URL}/me",
                    headers=headers,
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        used = sum(len(v.encode()) for v in self._cache.values())
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._cache),
        }
