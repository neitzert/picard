"""Matrix adapter — stores chunks as room messages via the Client-Server API.

Each chunk becomes an m.room.message event in the configured room.
The event_id is the chunk reference. Deletion uses Matrix redaction.

Requires: pip install picard[matrix]  (uses httpx)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class MatrixAdapter(Adapter):
    """Scatter chunks into Matrix room messages."""

    def __init__(
        self,
        homeserver: str,
        access_token: str,
        room_id: str,
        *,
        max_chunk_bytes: int = 60_000,
    ) -> None:
        self._homeserver = homeserver.rstrip("/")
        self._access_token = access_token
        self._room_id = room_id
        self._max = max_chunk_bytes
        self._base = f"{self._homeserver}/_matrix/client/v3"
        self._client: object | None = None

    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        import httpx

        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {self._access_token}"},
        )
        return self._client

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="matrix",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=60,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        client = await self._ensure_client()
        txn_id = uuid.uuid4().hex
        resp = await client.put(
            f"{self._base}/rooms/{self._room_id}/send/m.room.message/{txn_id}",
            json={"msgtype": "m.text", "body": encoded_chunk},
        )
        data = resp.json()
        return StoredChunkRef(adapter_name="matrix", location=data["event_id"])

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        client = await self._ensure_client()
        resp = await client.get(
            f"{self._base}/rooms/{self._room_id}/event/{ref.location}",
        )
        data = resp.json()
        return data["content"]["body"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        client = await self._ensure_client()
        try:
            txn_id = uuid.uuid4().hex
            resp = await client.put(
                f"{self._base}/rooms/{self._room_id}/redact/{ref.location}/{txn_id}",
                json={"reason": "picard chunk cleanup"},
            )
            return "event_id" in resp.json()
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._ensure_client()
        prefix = f"PCHK:1:{manifest_id}:"
        refs: list[StoredChunkRef] = []
        from_token: str | None = None

        while True:
            params: dict[str, str | int] = {"dir": "b", "limit": 100}
            if from_token:
                params["from"] = from_token
            resp = await client.get(
                f"{self._base}/rooms/{self._room_id}/messages",
                params=params,
            )
            data = resp.json()
            for event in data.get("chunk", []):
                if event.get("type") != "m.room.message":
                    continue
                body = event.get("content", {}).get("body", "")
                if body.startswith(prefix):
                    refs.append(
                        StoredChunkRef(
                            adapter_name="matrix",
                            location=event["event_id"],
                        )
                    )
            from_token = data.get("end")
            if not from_token or not data.get("chunk"):
                break
        return refs

    async def health_check(self) -> bool:
        try:
            client = await self._ensure_client()
            resp = await client.get(f"{self._base}/account/whoami")
            return "user_id" in resp.json()
        except Exception:
            return False
