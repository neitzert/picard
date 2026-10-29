"""Redis TimeSeries adapter -- stores chunks as labels on time-series keys.

Each write_chunk creates a TS.CREATE key with LABELS chunk <encoded> and
manifest <manifest_id>. Because TimeSeries labels have an 8KB limit on values
and cannot store arbitrary binary, we store the chunk as a base64url label
and the actual data as a sample (timestamp = 1, value = token hash as float).

Requires: pip install redis[hiredis] and Redis with RedisTimeSeries module.
"""
from __future__ import annotations

import base64
import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "redis-timeseries"
_PREFIX = "picard:ts:"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class RedisTimeseriesAdapter(Adapter):

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 6379,
        password: str = "",
        db: int = 0,
    ) -> None:
        self._host = host
        self._port = port
        self._password = password or None
        self._db = db
        self._client = None
        self._reg: dict[str, str] = {}

    async def _get_client(self):
        if self._client is None:
            import redis.asyncio as redis
            self._client = redis.Redis(
                host=self._host, port=self._port,
                password=self._password, db=self._db,
                decode_responses=True,
            )
        return self._client

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=4096,
            persistent=True,
            ordered=False,
            rate_limit_per_min=10000,
            tags={"requires": "redis[hiredis],redistimeseries-module",
                  "substrate": "redis-timeseries"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        key = f"{_PREFIX}{token}"
        encoded_b64 = base64.urlsafe_b64encode(
            encoded_chunk.encode()
        ).decode("ascii")
        client = await self._get_client()
        await client.execute_command(
            "TS.CREATE", key,
            "RETENTION", "0",
            "LABELS",
            "picard", "1",
            "manifest", manifest_id,
            "token", token,
            "chunk", encoded_b64,
        )
        # Add one sample so the key is queryable
        await client.execute_command("TS.ADD", key, 1, 1.0)
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
        client = await self._get_client()
        info = await client.execute_command("TS.INFO", loc["key"])
        # TS.INFO returns a flat list; parse to dict
        info_dict: dict[str, str] = {}
        it = iter(info)
        for k in it:
            v = next(it, None)
            info_dict[str(k)] = str(v) if v is not None else ""
        chunk_b64 = info_dict.get("chunk", "")
        if not chunk_b64:
            # Try labels sub-section
            labels_raw = info_dict.get("labels", "")
            for label_entry in (labels_raw if isinstance(labels_raw, list) else []):
                if isinstance(label_entry, (list, tuple)) and len(label_entry) == 2:
                    if str(label_entry[0]) == "chunk":
                        chunk_b64 = str(label_entry[1])
                        break
        if not chunk_b64:
            raise KeyError(f"redis-timeseries chunk not found: {token}")
        chunk = base64.urlsafe_b64decode(chunk_b64.encode()).decode()
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        client = await self._get_client()
        await client.delete(loc["key"])
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._get_client()
        try:
            keys = await client.execute_command(
                "TS.QUERYINDEX", f"manifest={manifest_id}", "picard=1"
            )
        except Exception:
            keys = []
        refs = []
        for key in keys:
            token = str(key)[len(_PREFIX):]
            refs.append(StoredChunkRef(
                adapter_name=_NAME,
                location=json.dumps({"token": token, "key": str(key),
                                      "manifest": manifest_id}),
            ))
        return refs

    async def health_check(self) -> bool:
        try:
            client = await self._get_client()
            await client.ping()
            hc_key = "__picard_ts_hc__"
            await client.execute_command(
                "TS.CREATE", hc_key, "RETENTION", "60000",
                "LABELS", "picard", "hc",
            )
            await client.delete(hc_key)
            return True
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
