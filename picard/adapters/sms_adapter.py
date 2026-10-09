"""SMS adapter — stores chunks as text messages via Twilio API.

Each chunk becomes an SMS or concatenated SMS.  Persistent in the
carrier's message log, billable, and looks like normal texting traffic.

Requires: pip install picard[sms]  (aiohttp)
"""

from __future__ import annotations

import base64
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk
import aiohttp


class SmsAdapter(Adapter):
    """Scatter chunks into SMS messages via Twilio."""

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        from_number: str,
        to_number: str,
        *,
        api_url: str = "https://api.twilio.com",
        max_chunk_bytes: int = 1600,
    ) -> None:
        self._sid = account_sid
        self._token = auth_token
        self._from = from_number
        self._to = to_number
        self._api = api_url.rstrip("/")
        self._max = max_chunk_bytes
        self._written: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="sms",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=30,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import aiohttp

        try:
            chunk = Chunk.decode(encoded_chunk)
            key = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
        except ValueError:
            key = uuid.uuid4().hex

        url = f"{self._api}/2010-04-01/Accounts/{self._sid}/Messages.json"
        auth = aiohttp.BasicAuth(self._sid, self._token)
        data = {
            "From": self._from,
            "To": self._to,
            "Body": encoded_chunk,
        }

        async with aiohttp.ClientSession(auth=auth) as session:
            async with session.post(url, data=data) as resp:
                resp.raise_for_status()
                result = await resp.json()

        msg_sid = result["sid"]
        loc = f"sms://{msg_sid}"
        self._written[loc] = msg_sid
        return StoredChunkRef(adapter_name="sms", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import aiohttp

        msg_sid = self._written.get(ref.location, ref.location.split("://", 1)[-1])
        url = f"{self._api}/2010-04-01/Accounts/{self._sid}/Messages/{msg_sid}.json"
        auth = aiohttp.BasicAuth(self._sid, self._token)

        async with aiohttp.ClientSession(auth=auth) as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                result = await resp.json()

        return result["body"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        import aiohttp

        msg_sid = self._written.pop(ref.location, ref.location.split("://", 1)[-1])
        url = f"{self._api}/2010-04-01/Accounts/{self._sid}/Messages/{msg_sid}.json"
        auth = aiohttp.BasicAuth(self._sid, self._token)

        try:
            async with aiohttp.ClientSession(auth=auth) as session:
                async with session.delete(url) as resp:
                    return resp.status in (200, 204)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="sms", location=loc)
            for loc in self._written
            if manifest_id in loc
        ]

    async def health_check(self) -> bool:
        import aiohttp

        try:
            url = f"{self._api}/2010-04-01/Accounts/{self._sid}.json"
            auth = aiohttp.BasicAuth(self._sid, self._token)
            async with aiohttp.ClientSession(auth=auth) as session:
                async with session.get(url) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        return {
            "used_bytes": None,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._written),
        }
