"""WhatsApp Business API adapter — stores chunks as WhatsApp messages.

Uses the Meta Cloud API.

Notice: This adapter must only be used with WhatsApp Business accounts and
phone numbers you own and control.  Use in a manner that violates Meta's
Terms of Service is outside the scope of this project and the responsibility
of the user.  No endorsement by or affiliation with Meta is implied.

Requires: pip install picard-store[whatsapp]  (uses aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_GRAPH_URL = "https://graph.facebook.com/v17.0"


class WhatsAppAdapter(Adapter):
    """Scatter chunks into WhatsApp messages."""

    def __init__(
        self,
        phone_number_id: str,
        access_token: str,
        recipient_number: str,
        *,
        max_chunk_bytes: int = 4096,
    ) -> None:
        self._phone_id = phone_number_id
        self._token = access_token
        self._recipient = recipient_number
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}
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
            name="whatsapp",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=30,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_id = uuid.uuid4().hex[:12]
        message = f"PICARD:{chunk_id}\n{encoded_chunk}"

        session = await self._get_session()
        async with session.post(
            f"{_GRAPH_URL}/{self._phone_id}/messages",
            headers={"Authorization": f"Bearer {self._token}"},
            json={
                "messaging_product": "whatsapp",
                "to": self._recipient,
                "type": "text",
                "text": {"body": message},
            },
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        msg_id = data.get("messages", [{}])[0].get("id", chunk_id)
        location = f"wa://{self._phone_id}/{msg_id}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="whatsapp", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]
        raise KeyError(f"chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="whatsapp", location=loc)
            for loc in self._cache
        ]

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get(
                f"{_GRAPH_URL}/{self._phone_id}",
                headers={"Authorization": f"Bearer {self._token}"},
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
