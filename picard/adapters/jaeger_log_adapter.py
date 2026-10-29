"""Jaeger span-log adapter -- stores chunks in structured log fields within Jaeger spans.

Each write_chunk creates an OTLP span with a log event containing the
encoded chunk in an 'event.data' field. Retrieval uses Jaeger query API.

Requires: pip install httpx
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "jaeger_log"
_OTLP_URL = "http://127.0.0.1:4318/v1/traces"
_QUERY_URL = "http://127.0.0.1:16686"
_SVC = "picard-log"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class JaegerLogAdapter(Adapter):

    def __init__(self, *, otlp_url: str = _OTLP_URL,
                 query_url: str = _QUERY_URL) -> None:
        self._otlp_url = otlp_url
        self._query_url = query_url

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=100_000,
            persistent=True,
            ordered=False,
            ttl_seconds=3600,
            tags={"requires": "httpx", "substrate": "jaeger-span-log"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import httpx
        trace_id = uuid.uuid4().hex  # 128-bit = 32 hex chars
        span_id = uuid.uuid4().hex[:16]
        now_ns = int(time.time() * 1e9)
        payload = {
            "resourceSpans": [{
                "resource": {"attributes": [
                    {"key": "service.name",
                     "value": {"stringValue": _SVC}},
                ]},
                "scopeSpans": [{
                    "spans": [{
                        "traceId": trace_id,
                        "spanId": span_id,
                        "name": "picard.log",
                        "kind": 1,
                        "startTimeUnixNano": str(now_ns),
                        "endTimeUnixNano": str(now_ns + 1000),
                        "events": [{
                            "timeUnixNano": str(now_ns),
                            "name": "picard.data",
                            "attributes": [
                                {"key": "event.data",
                                 "value": {"stringValue": encoded_chunk}},
                            ],
                        }],
                        "status": {},
                    }],
                }],
            }],
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(self._otlp_url, json=payload)
            resp.raise_for_status()
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"trace_id": trace_id,
                               "span_id": span_id}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import httpx
        loc = json.loads(ref.location)
        trace_id = loc["trace_id"]
        # Retry up to 10s for Jaeger ingestion
        for _ in range(20):
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{self._query_url}/api/traces/{trace_id}"
                )
            if resp.status_code == 200:
                data = resp.json()
                for span in (data.get("data") or [{}])[0].get("spans", []):
                    for log in span.get("logs", []):
                        for field in log.get("fields", []):
                            if field.get("key") == "event.data":
                                return field["value"]
            import asyncio
            await asyncio.sleep(0.5)
        raise KeyError(f"jaeger_log trace not found: {trace_id}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{self._query_url}/api/traces",
                params={"service": _SVC, "limit": 200},
            )
        if resp.status_code != 200:
            return []
        refs = []
        for trace in resp.json().get("data", []):
            trace_id = trace.get("traceID", "")
            for span in trace.get("spans", []):
                for log in span.get("logs", []):
                    for field in log.get("fields", []):
                        if field.get("key") == "event.data":
                            chunk = field["value"]
                            if _manifest_id_from_chunk(chunk) == manifest_id:
                                refs.append(StoredChunkRef(adapter_name=_NAME,
                                                           location=json.dumps({
                                                               "trace_id": trace_id,
                                                               "span_id": span["spanID"],
                                                           })))
        return refs

    async def health_check(self) -> bool:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self._query_url}/api/services")
            return resp.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        pass
