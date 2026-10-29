"""VictoriaMetrics tag adapter -- stores chunks as Prometheus metric label values.

Each write_chunk pushes a metric named picard_tag_{token} with the encoded
chunk stored as the picard_data label. VictoriaMetrics has a ~5 s flush delay
before writes become queryable via the query API; warm reads use the in-process
registry.

Requires: VictoriaMetrics reachable at the configured URL.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "victoria-metrics-tag"
_FLUSH_WAIT = 7


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


def _escape_label(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class VictoriaMetricsTagAdapter(Adapter):

    def __init__(self, *, url: str = "http://127.0.0.1:8428") -> None:
        self._url = url.rstrip("/")
        self._reg: dict[str, str] = {}
        self._session = None

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=1024,
            persistent=True,
            ordered=False,
            ttl_seconds=604800,
            tags={"requires": "victoria-metrics", "substrate": "vm-label"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        metric_name = f"picard_tag_{token}"
        ts_ms = int(time.time() * 1000)
        line = (
            f'{metric_name}{{picard_data="{_escape_label(encoded_chunk)}",'
            f'manifest="{_escape_label(manifest_id)}"}} 1 {ts_ms}\n'
        )
        session = await self._get_session()
        async with session.post(
            f"{self._url}/api/v1/import/prometheus",
            data=line.encode(),
            headers={"Content-Type": "text/plain"},
        ) as resp:
            resp.raise_for_status()
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "metric": metric_name,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]
        import asyncio
        session = await self._get_session()
        for _ in range(_FLUSH_WAIT):
            async with session.get(
                f"{self._url}/api/v1/query",
                params={"query": loc["metric"]},
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
            results = data.get("data", {}).get("result", [])
            if results:
                chunk = results[0]["metric"].get("picard_data", "")
                if chunk:
                    self._reg[token] = chunk
                    return chunk
            await asyncio.sleep(1)
        raise KeyError(f"victoria-metrics-tag chunk not found: {loc['metric']}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.post(
            f"{self._url}/api/v1/admin/tsdb/delete_series",
            params={"match[]": f'{loc["metric"]}{{}}'},
        ) as resp:
            resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token,
                                          "metric": f"picard_tag_{token}",
                                          "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get(f"{self._url}/health") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
