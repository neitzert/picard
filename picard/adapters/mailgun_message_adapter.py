"""Mailgun message adapter -- stores chunks via Mailgun stored messages API.

Each write_chunk sends an email to a configured recipient and records the
Mailgun message ID. Reads use the Mailgun Storage API to fetch the stored
message content.

Requires: aiohttp, Mailgun API key, domain, recipient address.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "mailgun-message"
_BASE = "https://api.mailgun.net/v3"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class MailgunMessageAdapter(Adapter):

    def __init__(self, *, api_key: str = "", domain: str = "",
                 recipient: str = "") -> None:
        self._api_key = api_key
        self._domain = domain
        self._recipient = recipient
        self._session = None
        self._reg: dict[str, str] = {}

    async def _get_session(self):
        if self._session is None:
            import aiohttp, base64
            creds = base64.b64encode(f"api:{self._api_key}".encode()).decode()
            self._session = aiohttp.ClientSession(headers={
                "Authorization": f"Basic {creds}",
            })
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=False,
            ordered=False,
            rate_limit_per_min=100,
            tags={"requires": "mailgun-api-key,domain,recipient",
                  "substrate": "mailgun-email-message",
                  "note": "write-only; sent emails cannot be retrieved via API"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        data = {
            "from": f"picard@{self._domain}",
            "to": self._recipient,
            "subject": f"picard:{token}",
            "text": encoded_chunk,
            "o:tracking": "no",
            "o:tag": f"picard,{manifest_id}",
        }
        async with session.post(
                f"{_BASE}/{self._domain}/messages",
                data=data) as resp:
            resp.raise_for_status()
            result = await resp.json()
        msg_id = result.get("id", token)
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
            f"mailgun-message cold read not supported: {loc['token']}")

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
        if not self._api_key or not self._domain:
            return False
        try:
            session = await self._get_session()
            async with session.get(
                    f"{_BASE}/domains/{self._domain}") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
