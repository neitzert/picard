"""IPFS Pinata pin adapter -- stores chunks via Pinata remote pinning service.

Each write_chunk pins a JSON object {token, chunk, manifest} to IPFS via
Pinata's pinJSONToIPFS endpoint. Cold reads fetch via the Pinata gateway.

Requires: aiohttp (already a PICARD dependency), Pinata API key + secret.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "ipfs-pin"
_PIN_URL = "https://api.pinata.cloud/pinning/pinJSONToIPFS"
_UNPIN_URL = "https://api.pinata.cloud/pinning/unpin"
_AUTH_URL = "https://api.pinata.cloud/data/testAuthentication"
_GW_URL = "https://gateway.pinata.cloud/ipfs"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class IpfsPinAdapter(Adapter):

    def __init__(self, *, api_key: str = "", api_secret: str = "") -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._reg: dict[str, str] = {}
        self._session = None

    def _headers(self) -> dict[str, str]:
        return {
            "pinata_api_key": self._api_key,
            "pinata_secret_api_key": self._api_secret,
            "Content-Type": "application/json",
        }

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=1_000_000,
            persistent=True,
            ordered=False,
            rate_limit_per_min=180,
            tags={"requires": "pinata-api-key", "substrate": "ipfs-pin"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        payload = {
            "pinataContent": {
                "token": token,
                "chunk": encoded_chunk,
                "manifest": manifest_id,
            },
            "pinataMetadata": {"name": f"picard-{token}"},
        }
        async with session.post(_PIN_URL, json=payload,
                                headers=self._headers()) as resp:
            resp.raise_for_status()
            data = await resp.json()
        cid = data["IpfsHash"]
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "cid": cid,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        async with session.get(f"{_GW_URL}/{loc['cid']}") as resp:
            resp.raise_for_status()
            data = await resp.json()
        chunk = data["chunk"]
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(f"{_UNPIN_URL}/{loc['cid']}",
                                   headers=self._headers()) as resp:
            resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token, "cid": "",
                                          "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        if not self._api_key or not self._api_secret:
            return False
        try:
            session = await self._get_session()
            async with session.get(_AUTH_URL,
                                    headers=self._headers()) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
