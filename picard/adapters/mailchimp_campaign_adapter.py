"""Mailchimp campaign adapter -- stores chunks as Mailchimp campaign HTML content.

Each write_chunk creates a campaign and sets its HTML content to the encoded
chunk. Cold reads fetch via GET /campaigns/{id}/content.

Requires: aiohttp, a Mailchimp API key and list ID. The datacenter is
extracted from the API key suffix (e.g. "abc123-us6" -> dc="us6").
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "mailchimp-campaign"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class MailchimpCampaignAdapter(Adapter):

    def __init__(self, *, api_key: str = "", list_id: str = "") -> None:
        self._api_key = api_key
        self._list_id = list_id
        self._reg: dict[str, str] = {}
        self._session = None

    def _base_url(self) -> str:
        dc = self._api_key.rsplit("-", 1)[-1] if "-" in self._api_key else "us1"
        return f"https://{dc}.api.mailchimp.com/3.0"

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            auth = aiohttp.BasicAuth("picard", self._api_key)
            self._session = aiohttp.ClientSession(auth=auth)
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=1_000_000,
            persistent=True,
            ordered=False,
            rate_limit_per_min=20,
            tags={"requires": "aiohttp,mailchimp-api-key", "substrate": "mailchimp-campaign-html"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        base = self._base_url()
        title = f"picard-{manifest_id[:16]}-{token}"
        create_payload = {
            "type": "regular",
            "settings": {
                "subject_line": title,
                "from_name": "picard",
                "reply_to": "picard@example.com",
                "title": title,
            },
            "recipients": {"list_id": self._list_id},
        }
        async with session.post(
                f"{base}/campaigns", json=create_payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
        campaign_id = data["id"]
        async with session.put(
                f"{base}/campaigns/{campaign_id}/content",
                json={"html": encoded_chunk}) as resp:
            resp.raise_for_status()
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "campaign_id": campaign_id,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        async with session.get(
                f"{self._base_url()}/campaigns/{loc['campaign_id']}/content") as resp:
            resp.raise_for_status()
            data = await resp.json()
        chunk = data["html"]
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(
                f"{self._base_url()}/campaigns/{loc['campaign_id']}") as resp:
            resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        if not self._api_key:
            return []
        session = await self._get_session()
        prefix = f"picard-{manifest_id[:16]}-"
        base = self._base_url()
        refs = []
        offset = 0
        while True:
            params = f"count=1000&offset={offset}&fields=campaigns.id,campaigns.settings.title"
            async with session.get(f"{base}/campaigns?{params}") as resp:
                if resp.status != 200:
                    break
                data = await resp.json()
            campaigns = data.get("campaigns", [])
            for c in campaigns:
                title = (c.get("settings") or {}).get("title", "")
                if title.startswith(prefix):
                    token = title[len(prefix):]
                    refs.append(StoredChunkRef(
                        adapter_name=_NAME,
                        location=json.dumps({"token": token,
                                              "campaign_id": c["id"],
                                              "manifest": manifest_id}),
                    ))
            if len(campaigns) < 1000:
                break
            offset += len(campaigns)
        return refs

    async def health_check(self) -> bool:
        if not self._api_key or not self._list_id:
            return False
        try:
            session = await self._get_session()
            async with session.get(f"{self._base_url()}/ping") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
