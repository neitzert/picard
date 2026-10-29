"""Consul KV adapter -- stores chunks in the Consul key-value store.

Each write_chunk PUTs the encoded chunk at key picard/<manifest>/<token> via
the Consul HTTP API. Reads use GET. Requires a running Consul agent.

Requires: aiohttp
"""
from __future__ import annotations

import base64
import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "consul-kv"
_PREFIX = "picard/"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class ConsulKvAdapter(Adapter):

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8500,
        token: str = "",
        scheme: str = "http",
    ) -> None:
        self._base = f"{scheme}://{host}:{port}/v1/kv"
        self._token = token
        self._session = None
        self._reg: dict[str, str] = {}

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {}
        if self._token:
            h["X-Consul-Token"] = self._token
        return h

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession(headers=self._headers())
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=512_000,
            persistent=True,
            ordered=False,
            rate_limit_per_min=5000,
            tags={"requires": "aiohttp,consul",
                  "substrate": "consul-kv"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        key = f"{_PREFIX}{manifest_id}/{token}"
        session = await self._get_session()
        async with session.put(
            f"{self._base}/{key}",
            data=encoded_chunk.encode("utf-8"),
        ) as resp:
            resp.raise_for_status()
        self._reg[token] = encoded_chunk
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
        session = await self._get_session()
        async with session.get(f"{self._base}/{loc['key']}") as resp:
            resp.raise_for_status()
            items = await resp.json()
        if not items:
            raise KeyError(f"consul-kv chunk not found: {token}")
        chunk = base64.b64decode(items[0]["Value"]).decode("utf-8")
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(f"{self._base}/{loc['key']}") as resp:
            resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        prefix = f"{_PREFIX}{manifest_id}/"
        session = await self._get_session()
        async with session.get(
            f"{self._base}/{prefix}", params={"keys": "true"}
        ) as resp:
            if resp.status == 404:
                return []
            resp.raise_for_status()
            keys = await resp.json()
        refs = []
        for key in (keys or []):
            token = key.split("/")[-1]
            refs.append(StoredChunkRef(
                adapter_name=_NAME,
                location=json.dumps({"token": token, "key": key,
                                      "manifest": manifest_id}),
            ))
        return refs

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            base_url = self._base.replace("/v1/kv", "/v1/status/leader")
            async with session.get(base_url) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
