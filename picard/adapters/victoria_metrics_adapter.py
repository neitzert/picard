"""VictoriaMetrics adapter -- stores chunks as metric label values.

Each write_chunk pushes a time-series sample to VictoriaMetrics where the
chunk is stored in a metric label value. Cold reads query via Prometheus API.

Requires: pip install aiohttp
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "victoria_metrics"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class VictoriaMetricsAdapter(Adapter):

    def __init__(self, *, base_url: str = "http://127.0.0.1:8428") -> None:
        self._base_url = base_url.rstrip("/")
        self._session = None
        self._cache: dict[str, str] = {}  # token -> chunk for hot reads before VM flushes

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=4096,
            persistent=True,
            ordered=False,
            ttl_seconds=86400 * 7,
            tags={"requires": "aiohttp", "substrate": "victoria-metrics-label"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        # Influx line protocol: metric_name,label=value value=1 timestamp_ns
        ts_ms = int(time.time() * 1000)
        # Escape special chars for Prometheus label values: \ " \n
        def _pesc(s: str) -> str:
            return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

        # Prometheus text format: metric_name{labels} value [timestamp_ms]
        line = (
            f'picard_chunk{{picard_token="{_pesc(token)}",'
            f'picard_manifest="{_pesc(manifest_id)}",'
            f'picard_data="{_pesc(encoded_chunk)}"}} 1 {ts_ms}'
        )
        session = await self._get_session()
        async with session.post(
            f"{self._base_url}/api/v1/import/prometheus",
            data=line,
            headers={"Content-Type": "text/plain"},
        ) as resp:
            resp.raise_for_status()
        self._cache[token] = encoded_chunk
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = json.loads(ref.location)["token"]
        if token in self._cache:
            return self._cache[token]
        session = await self._get_session()
        query = f'picard_chunk{{picard_token="{token}"}}'
        # VM flushes in-memory data on a ~5s interval; retry up to 10s
        for attempt in range(10):
            async with session.get(
                f"{self._base_url}/api/v1/query",
                params={"query": query},
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
            results = data.get("data", {}).get("result", [])
            if results:
                return results[0]["metric"]["picard_data"]
            import asyncio as _aio
            await _aio.sleep(1)
        raise KeyError(f"victoria_metrics chunk not found after 10s: {token}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = json.loads(ref.location)["token"]
        self._cache.pop(token, None)
        session = await self._get_session()
        match = f'{{picard_token="{token}"}}'
        async with session.post(
            f"{self._base_url}/api/v1/admin/tsdb/delete_series",
            params={"match[]": match},
        ) as resp:
            # 204 or 200 -- both fine
            pass
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        session = await self._get_session()
        query = f'picard_chunk{{picard_manifest="{manifest_id}"}}'
        async with session.get(
            f"{self._base_url}/api/v1/query",
            params={"query": query},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        refs = []
        for result in data.get("data", {}).get("result", []):
            token = result["metric"].get("picard_token", "")
            if token:
                refs.append(StoredChunkRef(adapter_name=_NAME,
                                           location=json.dumps({"token": token})))
        return refs

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get(f"{self._base_url}/health") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
