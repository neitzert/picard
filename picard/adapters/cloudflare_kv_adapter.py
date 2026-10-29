"""Cloudflare Workers KV adapter -- stores chunks in a Cloudflare KV namespace."""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "cloudflare-kv"
_BASE = "https://api.cloudflare.com/client/v4"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class CloudflareKvAdapter(Adapter):

    def __init__(self, *, account_id: str = "", namespace_id: str = "",
                 api_token: str = "") -> None:
        self._account_id = account_id
        self._namespace_id = namespace_id
        self._api_token = api_token
        self._session = None
        self._reg: dict[str, str] = {}

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._api_token:
            h["Authorization"] = f"Bearer {self._api_token}"
        return h

    def _ns_url(self) -> str:
        return (f"{_BASE}/accounts/{self._account_id}"
                f"/storage/kv/namespaces/{self._namespace_id}")

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession(headers=self._headers())
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=25_000_000,
            persistent=True,
            ordered=False,
            rate_limit_per_min=1200,
            tags={"requires": "aiohttp", "substrate": "cloudflare-kv"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        key = f"picard_{manifest_id}_{token}"
        self._reg[token] = encoded_chunk
        if self._account_id and self._namespace_id:
            session = await self._get_session()
            url = f"{self._ns_url()}/values/{key}"
            try:
                async with session.put(url, data=encoded_chunk) as resp:
                    resp.raise_for_status()
            except Exception:
                pass
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "key": key,
                                 "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]
        if self._account_id and self._namespace_id:
            session = await self._get_session()
            url = f"{self._ns_url()}/values/{loc['key']}"
            async with session.get(url) as resp:
                resp.raise_for_status()
                chunk = await resp.text()
            self._reg[token] = chunk
            return chunk
        raise KeyError(f"cloudflare-kv chunk not found: {token}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        if self._account_id and self._namespace_id:
            session = await self._get_session()
            url = f"{self._ns_url()}/values/{loc['key']}"
            try:
                async with session.delete(url) as resp:
                    return resp.status in (200, 404)
            except Exception:
                pass
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        if self._account_id and self._namespace_id:
            session = await self._get_session()
            url = f"{self._ns_url()}/keys"
            try:
                async with session.get(url, params={
                        "prefix": f"picard_{manifest_id}_"}) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                for item in data.get("result", []):
                    key = item["name"]
                    token = key.split("_")[-1]
                    refs.append(StoredChunkRef(
                        adapter_name=_NAME,
                        location=json.dumps({"token": token, "key": key,
                                             "manifest": manifest_id}),
                    ))
            except Exception:
                pass
        else:
            for token, chunk in self._reg.items():
                if _manifest_id_from_chunk(chunk) == manifest_id:
                    key = f"picard_{manifest_id}_{token}"
                    refs.append(StoredChunkRef(
                        adapter_name=_NAME,
                        location=json.dumps({"token": token, "key": key,
                                             "manifest": manifest_id}),
                    ))
        return refs

    async def health_check(self) -> bool:
        if not self._account_id:
            return True
        try:
            session = await self._get_session()
            url = (f"{_BASE}/accounts/{self._account_id}"
                   f"/storage/kv/namespaces")
            async with session.get(url) as resp:
                return resp.status in (200, 403)
        except Exception:
            return False

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
