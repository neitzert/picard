"""Datadog event adapter -- stores chunks as Datadog events.

Each write_chunk posts an event with the encoded chunk in the text field.
Datadog events are append-only; cold reads raise RuntimeError (no event API
for fetching by ID). Warm reads work via in-process registry.

Requires: aiohttp, a Datadog API key.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "datadog-event"
_BASE_URL = "https://api.datadoghq.com/api/v1"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class DatadogEventAdapter(Adapter):

    def __init__(self, *, api_key: str = "") -> None:
        self._api_key = api_key
        self._reg: dict[str, str] = {}
        self._session = None

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            headers = {}
            if self._api_key:
                headers["DD-API-KEY"] = self._api_key
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=4000,
            persistent=False,
            ordered=False,
            rate_limit_per_min=100,
            tags={"requires": "aiohttp,datadog-api-key", "substrate": "datadog-event-text",
                  "warm-read-only": "true"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        payload = {
            "title": f"picard-{token}",
            "text": encoded_chunk,
            "tags": [f"manifest:{manifest_id[:16]}", "source:picard"],
        }
        async with session.post(f"{_BASE_URL}/events", json=payload) as resp:
            resp.raise_for_status()
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        raise RuntimeError(
            "datadog-event: cold read not supported (append-only substrate)")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token,
                                          "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        if not self._api_key:
            return False
        try:
            session = await self._get_session()
            async with session.get(f"{_BASE_URL}/validate") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
