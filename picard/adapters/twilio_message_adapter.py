"""Twilio message adapter -- stores chunks as Twilio SMS message bodies.

Each write_chunk sends an SMS via the Twilio Messages API and stores the
message SID. Cold reads fetch the message body from the API.

Requires: aiohttp, a Twilio account SID, auth token, from/to phone numbers.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "twilio-message"
_BASE_URL = "https://api.twilio.com/2010-04-01"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class TwilioMessageAdapter(Adapter):

    def __init__(self, *, account_sid: str = "", auth_token: str = "",
                 from_phone: str = "", to_phone: str = "") -> None:
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._from_phone = from_phone
        self._to_phone = to_phone
        self._reg: dict[str, str] = {}
        self._session = None

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            auth = aiohttp.BasicAuth(self._account_sid, self._auth_token)
            self._session = aiohttp.ClientSession(auth=auth)
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=1600,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
            tags={"requires": "aiohttp,twilio-credentials", "substrate": "twilio-sms-body"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        data = {
            "Body": encoded_chunk,
            "From": self._from_phone,
            "To": self._to_phone,
        }
        url = f"{_BASE_URL}/Accounts/{self._account_sid}/Messages.json"
        async with session.post(url, data=data) as resp:
            resp.raise_for_status()
            body = await resp.json()
        msg_sid = body["sid"]
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "msg_sid": msg_sid,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        url = (f"{_BASE_URL}/Accounts/{self._account_sid}"
               f"/Messages/{loc['msg_sid']}.json")
        async with session.get(url) as resp:
            resp.raise_for_status()
            body = await resp.json()
        chunk = body["body"]
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        url = (f"{_BASE_URL}/Accounts/{self._account_sid}"
               f"/Messages/{loc['msg_sid']}.json")
        async with session.delete(url) as resp:
            resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token, "msg_sid": "",
                                          "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        if not self._account_sid or not self._auth_token:
            return False
        try:
            session = await self._get_session()
            url = f"{_BASE_URL}/Accounts/{self._account_sid}.json"
            async with session.get(url) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
