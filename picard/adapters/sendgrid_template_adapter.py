"""SendGrid template adapter -- stores chunks as dynamic template HTML content.

Each write_chunk creates a transactional template + version with the encoded
chunk as the HTML content. Cold reads fetch via GET /v3/templates/{id}.

Requires: aiohttp, a SendGrid API key.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "sendgrid-template"
_BASE_URL = "https://api.sendgrid.com"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class SendgridTemplateAdapter(Adapter):

    def __init__(self, *, api_key: str = "") -> None:
        self._api_key = api_key
        self._reg: dict[str, str] = {}
        self._session = None

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            headers = {}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=1_048_576,
            persistent=True,
            ordered=False,
            rate_limit_per_min=60,
            tags={"requires": "aiohttp,sendgrid-api-key", "substrate": "sendgrid-template-html"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        async with session.post(
                f"{_BASE_URL}/v3/templates",
                json={"name": f"picard-{token}", "generation": "dynamic"}) as resp:
            resp.raise_for_status()
            tmpl = await resp.json()
        tmpl_id = tmpl["id"]
        async with session.post(
                f"{_BASE_URL}/v3/templates/{tmpl_id}/versions",
                json={"name": "v1", "html_content": encoded_chunk,
                      "subject": f"picard-{token}", "active": 1}) as resp:
            resp.raise_for_status()
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "tmpl_id": tmpl_id,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        async with session.get(
                f"{_BASE_URL}/v3/templates/{loc['tmpl_id']}") as resp:
            resp.raise_for_status()
            data = await resp.json()
        chunk = data["versions"][0]["html_content"]
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(
                f"{_BASE_URL}/v3/templates/{loc['tmpl_id']}") as resp:
            resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token, "tmpl_id": "",
                                          "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        if not self._api_key:
            return False
        try:
            session = await self._get_session()
            async with session.get(f"{_BASE_URL}/v3/user/profile") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
