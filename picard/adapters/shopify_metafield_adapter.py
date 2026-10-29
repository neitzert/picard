"""Shopify metafield adapter -- stores chunks as Shopify shop metafields.

Each write_chunk creates a shop-level metafield with the encoded chunk as
the value. Cold reads fetch via GET /admin/api/2024-01/metafields/{id}.json.

Requires: aiohttp, a Shopify store domain and Admin API access token.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "shopify-metafield"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class ShopifyMetafieldAdapter(Adapter):

    def __init__(self, *, store_domain: str = "",
                 access_token: str = "") -> None:
        self._store_domain = store_domain
        self._access_token = access_token
        self._reg: dict[str, str] = {}
        self._session = None

    def _base_url(self) -> str:
        return f"https://{self._store_domain}/admin/api/2024-01"

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            headers = {}
            if self._access_token:
                headers["X-Shopify-Access-Token"] = self._access_token
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            rate_limit_per_min=40,
            tags={"requires": "aiohttp,shopify-access-token", "substrate": "shopify-shop-metafield"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        payload = {
            "metafield": {
                "namespace": "picard",
                "key": token,
                "value": encoded_chunk,
                "type": "multi_line_text_field",
            }
        }
        async with session.post(
                f"{self._base_url()}/metafields.json",
                json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
        mf_id = data["metafield"]["id"]
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "mf_id": mf_id,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        async with session.get(
                f"{self._base_url()}/metafields/{loc['mf_id']}.json") as resp:
            resp.raise_for_status()
            data = await resp.json()
        chunk = data["metafield"]["value"]
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(
                f"{self._base_url()}/metafields/{loc['mf_id']}.json") as resp:
            resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token, "mf_id": "",
                                          "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        if not self._store_domain or not self._access_token:
            return False
        try:
            session = await self._get_session()
            async with session.get(
                    f"{self._base_url()}/shop.json") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
