"""etcd key adapter -- stores chunks as etcd key-value entries.

Each write_chunk puts the encoded chunk at a key prefixed by /picard/<manifest>/<token>.
Cold reads use the etcd v3 gRPC REST gateway (etcd v3 API over HTTP/1.1 JSON).

Requires: aiohttp. etcd must expose the v3 gRPC-gateway (default port 2379).
"""
from __future__ import annotations

import base64
import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "etcd-key"
_PREFIX = "/picard/"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def _b64d(s: str) -> str:
    return base64.b64decode(s.encode()).decode()


class EtcdKeyAdapter(Adapter):

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 2379,
        scheme: str = "http",
    ) -> None:
        self._base = f"{scheme}://{host}:{port}/v3"
        self._session = None
        self._reg: dict[str, str] = {}

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=1_500_000,
            persistent=True,
            ordered=False,
            rate_limit_per_min=5000,
            tags={"requires": "aiohttp,etcd",
                  "substrate": "etcd-kv"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        key = f"{_PREFIX}{manifest_id}/{token}"
        session = await self._get_session()
        async with session.post(
            f"{self._base}/kv/put",
            json={"key": _b64(key), "value": _b64(encoded_chunk)},
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
        async with session.post(
            f"{self._base}/kv/range",
            json={"key": _b64(loc["key"])},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        kvs = data.get("kvs", [])
        if not kvs:
            raise KeyError(f"etcd-key chunk not found: {token}")
        chunk = _b64d(kvs[0]["value"])
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.post(
            f"{self._base}/kv/deleterange",
            json={"key": _b64(loc["key"])},
        ) as resp:
            resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        prefix = f"{_PREFIX}{manifest_id}/"
        # etcd range_end for a prefix: increment last byte of the prefix key
        end_bytes = bytearray(prefix.encode())
        end_bytes[-1] += 1
        range_end = base64.b64encode(bytes(end_bytes)).decode()
        session = await self._get_session()
        async with session.post(
            f"{self._base}/kv/range",
            json={
                "key": _b64(prefix),
                "range_end": range_end,
                "keys_only": True,
            },
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        refs = []
        for kv in data.get("kvs", []):
            key = _b64d(kv["key"])
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
            async with session.post(
                f"{self._base}/maintenance/status", json={}
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
