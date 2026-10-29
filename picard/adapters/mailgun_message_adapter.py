"""Mailgun message adapter -- stores chunks as Mailgun stored messages.

Each write_chunk sends a message via Mailgun with the encoded chunk as the
text body, and retrieves the storage URL from the response for cold reads.

Requires: aiohttp, a Mailgun API key and domain.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "mailgun-message"
_BASE_URL = "https://api.mailgun.net/v3"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class MailgunMessageAdapter(Adapter):

    def __init__(self, *, api_key: str = "", domain: str = "") -> None:
        self._api_key = api_key
        self._domain = domain
        self._reg: dict[str, str] = {}
        self._session = None

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            auth = aiohttp.BasicAuth("api", self._api_key)
            self._session = aiohttp.ClientSession(auth=auth)
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            rate_limit_per_min=60,
            tags={"requires": "aiohttp,mailgun-api-key", "substrate": "mailgun-message-body"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        data = {
            "from": f"picard@{self._domain}",
            "to": f"picard@{self._domain}",
            "subject": f"picard-{token}",
            "text": encoded_chunk,
            "o:tag": [f"manifest-{manifest_id[:8]}"],
        }
        async with session.post(
                f"{_BASE_URL}/{self._domain}/messages",
                data=data) as resp:
            resp.raise_for_status()
            body = await resp.json()
        storage_url = body.get("storage", {}).get("url", "")
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "storage_url": storage_url,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        if not loc.get("storage_url"):
            raise RuntimeError("mailgun-message: no storage URL for cold read")
        session = await self._get_session()
        async with session.get(loc["storage_url"]) as resp:
            resp.raise_for_status()
            data = await resp.json()
        chunk = data.get("body-plain", data.get("stripped-text", ""))
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        if loc.get("storage_url"):
            session = await self._get_session()
            async with session.delete(loc["storage_url"]) as resp:
                resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token, "storage_url": "",
                                          "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        if not self._api_key or not self._domain:
            return False
        try:
            session = await self._get_session()
            async with session.get(
                    f"{_BASE_URL}/{self._domain}") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
