"""Cisco Webex adapter — stores chunks as messages in a Webex space.

Requires: pip install picard[webex]  (uses aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_BASE_URL = "https://webexapis.com/v1"


class WebexAdapter(Adapter):
    """Scatter chunks into Cisco Webex space messages."""

    def __init__(
        self,
        access_token: str,
        room_id: str,
        *,
        max_chunk_bytes: int = 7439,
    ) -> None:
        self._token = access_token
        self._room_id = room_id
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

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="webex",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=60,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_id = uuid.uuid4().hex[:12]
        text = f"PICARD:{chunk_id}\n{encoded_chunk}"

        session = await self._get_session()
        async with session.post(
            f"{_BASE_URL}/messages",
            headers=self._headers(),
            json={"roomId": self._room_id, "markdown": text},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        msg_id = data["id"]
        location = f"webex://{self._room_id}/{msg_id}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="webex", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]

        msg_id = ref.location.rsplit("/", 1)[-1]

        session = await self._get_session()
        async with session.get(
            f"{_BASE_URL}/messages/{msg_id}",
            headers=self._headers(),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        text = data.get("markdown", data.get("text", ""))
        return text.split("\n", 1)[1]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        msg_id = ref.location.rsplit("/", 1)[-1]
        self._cache.pop(ref.location, None)
        try:
            session = await self._get_session()
            async with session.delete(
                f"{_BASE_URL}/messages/{msg_id}",
                headers=self._headers(),
            ) as resp:
                return resp.status in (200, 204)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        prefix = f"PCHK:1:{manifest_id}:"
        refs: list[StoredChunkRef] = []

        session = await self._get_session()
        async with session.get(
            f"{_BASE_URL}/messages",
            headers=self._headers(),
            params={"roomId": self._room_id, "max": 200},
        ) as resp:
            if resp.status != 200:
                return refs
            data = await resp.json()

        for msg in data.get("items", []):
            text = msg.get("text", "")
            if prefix in text:
                refs.append(
                    StoredChunkRef(adapter_name="webex", location=f"webex://{self._room_id}/{msg['id']}")
                )

        return refs

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get(
                f"{_BASE_URL}/people/me",
                headers=self._headers(),
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
