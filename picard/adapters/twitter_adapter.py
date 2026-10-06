"""Twitter/X adapter — stores chunks as tweets or DMs.

Each chunk becomes a tweet or direct message:
    twitter://{tweet_id}  or  twitter://dm/{dm_id}

Requires: pip install picard[twitter]
"""

from __future__ import annotations

from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_API = "https://api.twitter.com/2"


class TwitterAdapter(Adapter):
    """Scatter chunks into Twitter/X tweets or DMs."""

    def __init__(
        self,
        bearer_token: str,
        *,
        access_token: str | None = None,
        access_secret: str | None = None,
        api_key: str | None = None,
        api_secret: str | None = None,
        use_dm: bool = False,
        dm_recipient_id: str | None = None,
        max_chunk_bytes: int | None = None,
    ) -> None:
        self._bearer = bearer_token
        self._access_token = access_token
        self._access_secret = access_secret
        self._api_key = api_key
        self._api_secret = api_secret
        self._use_dm = use_dm
        self._dm_recipient_id = dm_recipient_id
        self._max = max_chunk_bytes or (10_000 if use_dm else 280)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="twitter",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=15,
        )

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._bearer}",
            "Content-Type": "application/json",
        }

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import aiohttp

        if self._use_dm and self._dm_recipient_id:
            body = {
                "text": encoded_chunk,
                "participant_id": self._dm_recipient_id,
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{_API}/dm_conversations/with/{self._dm_recipient_id}/messages",
                    json=body,
                    headers=self._headers(),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    dm_id = data["data"]["dm_event_id"]
            location = f"twitter://dm/{dm_id}"
        else:
            body = {"text": encoded_chunk}
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{_API}/tweets", json=body, headers=self._headers()
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    tweet_id = data["data"]["id"]
            location = f"twitter://{tweet_id}"

        return StoredChunkRef(adapter_name="twitter", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import aiohttp

        path = ref.location.split("://", 1)[1]

        if path.startswith("dm/"):
            dm_id = path.split("/", 1)[1]
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{_API}/dm_events/{dm_id}",
                    headers=self._headers(),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
            return data["data"]["text"]
        else:
            tweet_id = path
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{_API}/tweets/{tweet_id}",
                    headers=self._headers(),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
            return data["data"]["text"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        import aiohttp

        path = ref.location.split("://", 1)[1]

        if path.startswith("dm/"):
            return False

        tweet_id = path
        try:
            async with aiohttp.ClientSession() as session:
                async with session.delete(
                    f"{_API}/tweets/{tweet_id}", headers=self._headers()
                ) as resp:
                    data = await resp.json()
                    return data.get("data", {}).get("deleted", False)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{_API}/users/me", headers=self._headers()
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False
