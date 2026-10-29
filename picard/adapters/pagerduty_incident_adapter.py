"""PagerDuty incident adapter -- stores chunks as PagerDuty incident bodies.

Each write_chunk creates an incident with the encoded chunk as the body
details. Cold reads fetch via GET /incidents/{id}. Delete resolves
the incident (PagerDuty does not allow true deletion).

Requires: aiohttp, a PagerDuty API token, service ID, and from email.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "pagerduty-incident"
_BASE_URL = "https://api.pagerduty.com"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class PagerdutyIncidentAdapter(Adapter):

    def __init__(self, *, api_token: str = "", service_id: str = "",
                 from_email: str = "picard@example.com") -> None:
        self._api_token = api_token
        self._service_id = service_id
        self._from_email = from_email
        self._reg: dict[str, str] = {}
        self._session = None

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            headers = {
                "Accept": "application/vnd.pagerduty+json;version=2",
                "Content-Type": "application/json",
            }
            if self._api_token:
                headers["Authorization"] = f"Token token={self._api_token}"
            if self._from_email:
                headers["From"] = self._from_email
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            rate_limit_per_min=60,
            tags={"requires": "aiohttp,pagerduty-api-token", "substrate": "pagerduty-incident-body"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        payload = {
            "incident": {
                "type": "incident",
                "title": f"picard-{token}",
                "service": {"id": self._service_id,
                             "type": "service_reference"},
                "body": {"type": "incident_body",
                         "details": encoded_chunk},
            }
        }
        async with session.post(f"{_BASE_URL}/incidents",
                                json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
        inc_id = data["incident"]["id"]
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "inc_id": inc_id,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        async with session.get(
                f"{_BASE_URL}/incidents/{loc['inc_id']}") as resp:
            resp.raise_for_status()
            data = await resp.json()
        chunk = data["incident"]["body"]["details"]
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        payload = {"incident": {"type": "incident", "status": "resolved"}}
        async with session.put(
                f"{_BASE_URL}/incidents/{loc['inc_id']}",
                json=payload) as resp:
            resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token, "inc_id": "",
                                          "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        if not self._api_token or not self._service_id:
            return False
        try:
            session = await self._get_session()
            async with session.get(f"{_BASE_URL}/users/me") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
