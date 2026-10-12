"""Grafana Loki adapter -- stores chunks as log entries.

Data is pushed into Loki log streams as structured log entries.

Requires: pip install picard-store[loki]  (uses aiohttp)
"""

from __future__ import annotations

import time
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
import aiohttp


class LokiAdapter(Adapter):
    """Scatter chunks into Grafana Loki log streams."""

    def __init__(
        self,
        loki_url: str,
        *,
        labels: dict[str, str] | None = None,
        tenant_id: str | None = None,
        username: str | None = None,
        password: str | None = None,
        max_chunk_bytes: int = 8192,
    ) -> None:
        self._loki_url = loki_url.rstrip("/")
        self._labels = labels or {"job": "picard", "source": "store"}
        self._tenant_id = tenant_id
        self._username = username
        self._password = password
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}
        self._session = None


    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="loki",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=120,
            ttl_seconds=None,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_id = uuid.uuid4().hex[:12]
        ts = str(int(time.time() * 1e9))  # nanosecond timestamp
        label_str = ",".join(f'{k}="{v}"' for k, v in self._labels.items())
        payload = {
            "streams": [
                {
                    "stream": self._labels,
                    "values": [[ts, f"PICARD:{chunk_id}:{encoded_chunk}"]],
                }
            ]
        }
        headers = {"Content-Type": "application/json"}
        if self._tenant_id:
            headers["X-Scope-OrgID"] = self._tenant_id

        auth = None
        if self._username and self._password:
            auth = aiohttp.BasicAuth(self._username, self._password)

        session = await self._get_session()
        async with session.post(
            f"{self._loki_url}/loki/api/v1/push",
            json=payload,
            headers=headers,
            auth=auth,
        ) as resp:
            resp.raise_for_status()

        location = f"loki://{chunk_id}@{ts}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="loki", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]
        raise KeyError(f"chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="loki", location=loc)
            for loc in self._cache
        ]

    async def health_check(self) -> bool:
        try:
            headers = {}
            if self._tenant_id:
                headers["X-Scope-OrgID"] = self._tenant_id
            auth = None
            if self._username and self._password:
                auth = aiohttp.BasicAuth(self._username, self._password)
            session = await self._get_session()
            async with session.get(
                f"{self._loki_url}/ready",
                headers=headers,
                auth=auth,
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        used = sum(len(v.encode()) for v in self._cache.values())
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._cache),
        }
