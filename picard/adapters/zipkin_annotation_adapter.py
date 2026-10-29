"""Zipkin annotation adapter -- stores chunks in Zipkin span annotation values.

Each write_chunk posts a Zipkin span with an annotation whose value holds the
encoded chunk. Retrieval queries Zipkin's /api/v2/trace endpoint.

Requires: pip install httpx
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "zipkin_annotation"
_ZIPKIN_URL = "http://127.0.0.1:9411"
_SVC = "picard-annotation"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class ZipkinAnnotationAdapter(Adapter):

    def __init__(self, *, url: str = _ZIPKIN_URL) -> None:
        self._url = url

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=100_000,
            persistent=True,
            ordered=False,
            ttl_seconds=3600,
            tags={"requires": "httpx", "substrate": "zipkin-annotation"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import httpx
        trace_id = uuid.uuid4().hex
        span_id = uuid.uuid4().hex[:16]
        now_us = int(time.time() * 1e6)
        payload = [{
            "traceId": trace_id,
            "id": span_id,
            "name": "picard.annotation",
            "timestamp": now_us,
            "duration": 1,
            "localEndpoint": {"serviceName": _SVC},
            "annotations": [{
                "timestamp": now_us,
                "value": encoded_chunk,
            }],
        }]
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{self._url}/api/v2/spans", json=payload)
            resp.raise_for_status()
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"trace_id": trace_id,
                               "span_id": span_id}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import httpx
        loc = json.loads(ref.location)
        trace_id = loc["trace_id"]
        for _ in range(20):
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{self._url}/api/v2/trace/{trace_id}")
            if resp.status_code == 200:
                for span in resp.json():
                    for ann in span.get("annotations", []):
                        val = ann.get("value", "")
                        if val.startswith("PCHK:"):
                            return val
            await asyncio.sleep(0.5)
        raise KeyError(f"zipkin_annotation trace not found: {trace_id}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{self._url}/api/v2/traces",
                params={"serviceName": _SVC, "limit": 200},
            )
        if resp.status_code != 200:
            return []
        refs = []
        for trace in resp.json():
            for span in trace:
                trace_id = span.get("traceId", "")
                for ann in span.get("annotations", []):
                    val = ann.get("value", "")
                    if _manifest_id_from_chunk(val) == manifest_id:
                        refs.append(StoredChunkRef(adapter_name=_NAME,
                                                   location=json.dumps({
                                                       "trace_id": trace_id,
                                                       "span_id": span["id"],
                                                   })))
        return refs

    async def health_check(self) -> bool:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self._url}/api/v2/services")
            return resp.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        pass
