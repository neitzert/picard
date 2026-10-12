"""Google Cloud Pub/Sub adapter — stores chunks as Pub/Sub messages.

Publishes chunks to a GCP Pub/Sub topic. Like SNS, it's fire-and-forget —
subscribers receive the data when it arrives. Messages can be retained
for up to 31 days with message retention enabled.

Requires: pip install picard-store[gcp]  (aiohttp)
"""

from __future__ import annotations

import base64
import json
import uuid
from typing import Sequence

import aiohttp

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_PUBSUB_API = "https://pubsub.googleapis.com/v1"


class GooglePubSubAdapter(Adapter):
    """Scatter chunks via Google Cloud Pub/Sub messages."""

    def __init__(
        self,
        project_id: str,
        topic_id: str,
        *,
        access_token: str | None = None,
        subscription_id: str | None = None,
        max_chunk_bytes: int = 10485760,
    ) -> None:
        self._project = project_id
        self._topic = topic_id
        self._token = access_token
        self._subscription = subscription_id
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}
        self._session = None


    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="google-pubsub",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=600,
            tags={"cloud": "gcp"},
        )

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    @property
    def _topic_path(self) -> str:
        return f"projects/{self._project}/topics/{self._topic}"

    @property
    def _subscription_path(self) -> str | None:
        if self._subscription:
            return f"projects/{self._project}/subscriptions/{self._subscription}"
        return None

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            key = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
        except ValueError:
            key = uuid.uuid4().hex

        b64_data = base64.b64encode(encoded_chunk.encode()).decode()

        session = await self._get_session()
        resp = await session.post(
            f"{_PUBSUB_API}/{self._topic_path}:publish",
            headers=self._headers(),
            json={
                "messages": [{
                    "data": b64_data,
                    "attributes": {"picard_key": key},
                }],
            },
        )
        resp.raise_for_status()
        data = await resp.json()

        msg_ids = data.get("messageIds", [key])
        msg_id = msg_ids[0] if msg_ids else key
        loc = f"pubsub://{self._topic_path}/{msg_id}"
        self._cache[loc] = encoded_chunk
        return StoredChunkRef(adapter_name="google-pubsub", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]

        if not self._subscription_path:
            raise KeyError(f"No subscription configured; chunk not in cache: {ref.location}")

        session = await self._get_session()
        resp = await session.post(
            f"{_PUBSUB_API}/{self._subscription_path}:pull",
            headers=self._headers(),
            json={"maxMessages": 100},
        )
        resp.raise_for_status()
        data = await resp.json()

        msg_id = ref.location.split("/")[-1]
        for msg in data.get("receivedMessages", []):
            if msg.get("message", {}).get("messageId") == msg_id:
                raw = msg["message"]["data"]
                return base64.b64decode(raw).decode()

        raise KeyError(f"message not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="google-pubsub", location=loc)
            for loc in self._cache
            if manifest_id in loc
        ]

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            resp = await session.get(
                f"{_PUBSUB_API}/{self._topic_path}",
                headers=self._headers(),
            )
            return resp.status == 200
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        return {
            "used_bytes": sum(len(v.encode()) for v in self._cache.values()),
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._cache),
        }
