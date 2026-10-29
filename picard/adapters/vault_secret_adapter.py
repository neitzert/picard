"""HashiCorp Vault secret adapter -- stores chunks as KV v2 secrets.

Each write_chunk writes the encoded chunk to path secret/data/picard/<manifest>/<token>
using the KV secrets engine v2 API. Requires a Vault instance with KV v2 mounted
at "secret/".

Requires: aiohttp, Vault token with write access to secret/data/picard/*
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "vault-secret"
_MOUNT = "secret"
_PREFIX = "picard"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class VaultSecretAdapter(Adapter):

    def __init__(
        self,
        *,
        addr: str = "http://127.0.0.1:8200",
        token: str = "",
        mount: str = _MOUNT,
    ) -> None:
        self._addr = addr.rstrip("/")
        self._token = token
        self._mount = mount
        self._session = None
        self._reg: dict[str, str] = {}

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._token:
            h["X-Vault-Token"] = self._token
        return h

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession(headers=self._headers())
        return self._session

    def _data_url(self, manifest_id: str, token: str) -> str:
        return (
            f"{self._addr}/v1/{self._mount}/data"
            f"/{_PREFIX}/{manifest_id}/{token}"
        )

    def _list_url(self, manifest_id: str) -> str:
        return (
            f"{self._addr}/v1/{self._mount}/metadata"
            f"/{_PREFIX}/{manifest_id}/"
        )

    def _meta_url(self, manifest_id: str, token: str) -> str:
        return (
            f"{self._addr}/v1/{self._mount}/metadata"
            f"/{_PREFIX}/{manifest_id}/{token}"
        )

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=1_000_000,
            persistent=True,
            ordered=False,
            rate_limit_per_min=1000,
            tags={"requires": "aiohttp,vault-token",
                  "substrate": "vault-kv-v2"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        async with session.post(
            self._data_url(manifest_id, token),
            json={"data": {"chunk": encoded_chunk, "manifest": manifest_id}},
        ) as resp:
            resp.raise_for_status()
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]
        session = await self._get_session()
        async with session.get(
            self._data_url(loc["manifest"], token)
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        chunk = data["data"]["data"]["chunk"]
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(
            self._meta_url(loc["manifest"], loc["token"])
        ) as resp:
            return resp.status in (200, 204, 404)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        session = await self._get_session()
        async with session.request(
            "LIST", self._list_url(manifest_id)
        ) as resp:
            if resp.status == 404:
                return []
            resp.raise_for_status()
            data = await resp.json()
        refs = []
        for key in data.get("data", {}).get("keys", []):
            token = key.rstrip("/")
            refs.append(StoredChunkRef(
                adapter_name=_NAME,
                location=json.dumps({"token": token,
                                      "manifest": manifest_id}),
            ))
        return refs

    async def health_check(self) -> bool:
        if not self._token:
            return False
        try:
            session = await self._get_session()
            async with session.get(
                f"{self._addr}/v1/sys/health"
            ) as resp:
                return resp.status in (200, 429, 473)
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
