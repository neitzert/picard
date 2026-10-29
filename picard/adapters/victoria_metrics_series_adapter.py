"""VictoriaMetrics series adapter -- stores chunks as multi-point time series.

Each byte of the encoded chunk is written as a separate data point in a
metric picard_series_{token}, with a monotonically increasing timestamp.
The byte value is the metric value; the byte index is recovered by sorting
on timestamp. VictoriaMetrics has a ~5 s flush delay; warm reads use the
in-process registry.

Requires: VictoriaMetrics reachable at the configured URL.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "victoria-metrics-series"
_FLUSH_WAIT = 8
_TS_STEP_MS = 1000


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class VictoriaMetricsSeriesAdapter(Adapter):

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
            max_chunk_bytes=512,
            persistent=True,
            ordered=False,
            ttl_seconds=604800,
            tags={"requires": "victoria-metrics", "substrate": "vm-series"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        metric_name = f"picard_series_{token}"
        raw = encoded_chunk.encode("utf-8")
        n = len(raw)
        base_ts_ms = int(time.time() * 1000)
        lines = []
        for i, byte_val in enumerate(raw):
            ts_ms = base_ts_ms + i * _TS_STEP_MS
            lines.append(
                f'{metric_name}{{manifest="{manifest_id}"}} {byte_val} {ts_ms}'
            )
        body = "\n".join(lines) + "\n"
        session = await self._get_session()
        async with session.post(
            f"{self._url}/api/v1/import/prometheus",
            data=body.encode(),
            headers={"Content-Type": "text/plain"},
        ) as resp:
            resp.raise_for_status()
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({
                "token": token,
                "metric": metric_name,
                "manifest": manifest_id,
                "base_ts_ms": base_ts_ms,
                "n": n,
            }),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]
        session = await self._get_session()
        base = loc["base_ts_ms"] / 1000.0
        end = base + loc["n"] * (_TS_STEP_MS / 1000.0) + 1
        for _ in range(_FLUSH_WAIT):
            async with session.get(
                f"{self._url}/api/v1/query_range",
                params={
                    "query": loc["metric"],
                    "start": f"{base:.3f}",
                    "end": f"{end:.3f}",
                    "step": "1",
                },
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
            values = data.get("data", {}).get("result", [])
            if values:
                points = sorted(values[0].get("values", []), key=lambda p: float(p[0]))
                if len(points) >= loc["n"]:
                    byte_arr = bytes(int(p[1]) for p in points[: loc["n"]])
                    chunk = byte_arr.decode("utf-8")
                    self._reg[token] = chunk
                    return chunk
            await asyncio.sleep(1)
        raise KeyError(f"victoria-metrics-series chunk not found: {loc['metric']}")

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
                    location=json.dumps({
                        "token": token,
                        "metric": f"picard_series_{token}",
                        "manifest": manifest_id,
                        "base_ts_ms": 0,
                        "n": len(chunk.encode()),
                    }),
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
