"""VictoriaMetrics metric-name adapter -- stores chunks encoded in metric names.

Each write_chunk hex-encodes the chunk payload and stores it as part of the
metric name, using the picard_token label for retrieval. Cold reads query
VictoriaMetrics by the token label.

Requires: aiohttp; a running VictoriaMetrics instance on localhost:8428
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "victoria-metrics-metric"
_MAX_METRIC_NAME = 256


def _manifest_id(chunk: str) -> str:
    try:
        parts = chunk.split(":")
        return parts[2] if parts[0] == "PCHK" and len(parts) >= 3 else ""
    except Exception:
        return ""


def _encode_name(chunk: str) -> str:
    hex_chunk = chunk.encode("utf-8").hex()
    if len(hex_chunk) > _MAX_METRIC_NAME - 8:
        hex_chunk = hex_chunk[:_MAX_METRIC_NAME - 8]
    return f"pcd_{hex_chunk}"


class VictoriaMetricsMetricAdapter(Adapter):

    def __init__(self, *, url: str = "http://127.0.0.1:8428") -> None:
        self._url = url.rstrip("/")
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
            max_chunk_bytes=128,
            persistent=True,
            ordered=False,
            tags={"requires": "aiohttp", "substrate": "victoria-metrics-metric-name"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id(encoded_chunk)
        metric_name = _encode_name(encoded_chunk)
        ts = int(time.time() * 1000)
        line = f'{metric_name}{{picard_token="{token}",picard_manifest="{manifest_id}"}} 1 {ts}\n'
        session = await self._get_session()
        async with session.post(
            f"{self._url}/api/v1/import/prometheus",
            data=line.encode("utf-8"),
            headers={"Content-Type": "text/plain"},
        ) as resp:
            if resp.status not in (200, 204):
                text = await resp.text()
                raise RuntimeError(f"VictoriaMetrics error {resp.status}: {text}")
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "metric_name": metric_name,
                                  "chunk": encoded_chunk, "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]
        chunk = loc.get("chunk", "")
        if chunk:
            self._reg[token] = chunk
            return chunk
        # Query by token label
        session = await self._get_session()
        query = f'{{picard_token="{token}"}}'
        async with session.get(
            f"{self._url}/api/v1/query",
            params={"query": query},
        ) as resp:
            data = await resp.json()
        results = data.get("data", {}).get("result", [])
        if not results:
            raise KeyError(f"victoria-metrics-metric chunk not found: {token}")
        metric_name = results[0]["metric"].get("__name__", "")
        hex_chunk = metric_name[4:]  # strip pcd_ prefix
        recovered = bytes.fromhex(hex_chunk).decode("utf-8")
        self._reg[token] = recovered
        return recovered

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token,
                                          "metric_name": _encode_name(chunk),
                                          "chunk": chunk,
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
