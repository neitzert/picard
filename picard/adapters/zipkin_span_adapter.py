"""Zipkin span adapter -- stores chunks as Zipkin span tag values.

Each write_chunk submits a span to the Zipkin HTTP collector. The encoded
chunk is stored as a span tag. Cold reads query via the Zipkin API.

Requires: pip install aiohttp
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "zipkin_span"
_SERVICE = "picard"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


def _make_trace_id(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:32]


class ZipkinSpanAdapter(Adapter):

    def __init__(self, *, base_url: str = "http://127.0.0.1:9411") -> None:
        self._base_url = base_url.rstrip("/")
        self._session = None

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
            ttl_seconds=86400,
            tags={"requires": "aiohttp", "substrate": "zipkin-span-tag"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex
        trace_id = _make_trace_id(token)
        span_id = token[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        ts_us = int(time.time() * 1_000_000)
        span = [{
            "traceId": trace_id,
            "id": span_id,
            "name": "picard.chunk",
            "timestamp": ts_us,
            "duration": 1000,
            "localEndpoint": {"serviceName": _SERVICE},
            "tags": {
                "picard.token": token,
                "picard.manifest": manifest_id,
                "picard.data": encoded_chunk,
            },
        }]
        session = await self._get_session()
        async with session.post(
            f"{self._base_url}/api/v2/spans",
            json=span,
            headers={"Content-Type": "application/json"},
        ) as resp:
            resp.raise_for_status()
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token,
                               "trace_id": trace_id}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        trace_id = loc["trace_id"]
        session = await self._get_session()
        # Zipkin has up to a few seconds ingestion delay; retry
        for _ in range(10):
            async with session.get(
                f"{self._base_url}/api/v2/trace/{trace_id}",
            ) as resp:
                if resp.status == 200:
                    spans = await resp.json()
                    for span in spans:
                        data = span.get("tags", {}).get("picard.data")
                        if data:
                            return data
            import asyncio as _aio
            await _aio.sleep(1)
        raise KeyError(f"zipkin_span chunk not found: {trace_id}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        # Zipkin OSS does not expose a delete API
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        session = await self._get_session()
        async with session.get(
            f"{self._base_url}/api/v2/traces",
            params={"serviceName": _SERVICE, "tags": f"picard.manifest={manifest_id}",
                    "limit": 1000},
        ) as resp:
            if resp.status != 200:
                return []
            traces = await resp.json()
        refs = []
        for trace in traces:
            for span in trace:
                token = span.get("tags", {}).get("picard.token", "")
                trace_id = span.get("traceId", "")
                if token and trace_id:
                    refs.append(StoredChunkRef(adapter_name=_NAME,
                                               location=json.dumps({"token": token,
                                                "trace_id": trace_id})))
                    break
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
