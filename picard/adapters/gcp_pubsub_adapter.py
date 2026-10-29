"""Google Cloud Pub/Sub adapter -- stores chunks by publishing to a topic.

Each write_chunk publishes a base64-encoded message to a Pub/Sub topic.
The chunk is stored in _reg for reads; this adapter is write-oriented
(published messages are consumed by subscribers, not retrieved by publisher).

Requires: aiohttp, GCP Bearer token, project ID, topic name.
"""
from __future__ import annotations

import base64
import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "gcp-pubsub"
_BASE = "https://pubsub.googleapis.com/v1"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class GcpPubsubAdapter(Adapter):

    def __init__(self, *, access_token: str = "", project_id: str = "",
                 topic_name: str = "picard") -> None:
        self._token = access_token
        self._project_id = project_id
        self._topic = topic_name
        self._session = None
        self._reg: dict[str, str] = {}

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json"}

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    def _topic_path(self) -> str:
        return f"projects/{self._project_id}/topics/{self._topic}"

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=10_000_000,
            persistent=False,
            ordered=False,
            rate_limit_per_min=1000,
            tags={"requires": "gcp-bearer-token,project-id",
                  "substrate": "gcp-pubsub-message"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        data_b64 = base64.b64encode(encoded_chunk.encode()).decode()
        session = await self._get_session()
        async with session.post(
                f"{_BASE}/{self._topic_path()}:publish",
                headers=self._headers(),
                json={"messages": [{"data": data_b64,
                                    "attributes": {"token": token,
                                                   "manifest": manifest_id}}]}) as resp:
            resp.raise_for_status()
            data = await resp.json()
        msg_id = data["messageIds"][0]
        self._reg[token] = encoded_chunk
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token,
                                                    "msg_id": msg_id,
                                                    "manifest": manifest_id}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        raise KeyError(
            f"gcp-pubsub chunk not retrievable after publish: {loc['token']}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [StoredChunkRef(adapter_name=_NAME,
                               location=json.dumps({"token": t, "msg_id": "",
                                                     "manifest": manifest_id}))
                for t, c in self._reg.items() if _manifest_id_from_chunk(c) == manifest_id]

    async def health_check(self) -> bool:
        if not self._token or not self._project_id:
            return False
        try:
            session = await self._get_session()
            async with session.get(
                    f"{_BASE}/projects/{self._project_id}/topics",
                    headers=self._headers()) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
