"""OpenTelemetry log adapter -- stores chunks via OTLP HTTP logs endpoint.

Each write_chunk POSTs a log record to /v1/logs on an OTLP HTTP receiver
(e.g. Jaeger all-in-one on port 4318). The chunk is stored in the log body;
picard.token and picard.manifest attributes identify it. Cold reads use the
warm _reg cache or location JSON (OTLP logs are fire-and-forget; a backend
is needed for durable retrieval).

Requires: aiohttp; a running OTLP HTTP receiver on localhost:4318
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "opentelemetry-log"


def _manifest_id(chunk: str) -> str:
    try:
        parts = chunk.split(":")
        return parts[2] if parts[0] == "PCHK" and len(parts) >= 3 else ""
    except Exception:
        return ""


class OpentelemetryLogAdapter(Adapter):

    def __init__(self, *, url: str = "http://127.0.0.1:4318") -> None:
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
            max_chunk_bytes=65536,
            persistent=False,
            ordered=False,
            tags={"requires": "aiohttp", "substrate": "otlp-log-record"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id(encoded_chunk)
        ts_ns = int(time.time() * 1e9)
        payload = {
            "resourceLogs": [{
                "resource": {
                    "attributes": [
                        {"key": "service.name",
                         "value": {"stringValue": "picard"}},
                    ],
                },
                "scopeLogs": [{
                    "scope": {"name": "picard.adapters"},
                    "logRecords": [{
                        "timeUnixNano": str(ts_ns),
                        "body": {"stringValue": encoded_chunk},
                        "attributes": [
                            {"key": "picard.token",
                             "value": {"stringValue": token}},
                            {"key": "picard.manifest",
                             "value": {"stringValue": manifest_id}},
                        ],
                    }],
                }],
            }],
        }
        session = await self._get_session()
        async with session.post(
            f"{self._url}/v1/logs",
            json=payload,
            headers={"Content-Type": "application/json"},
        ) as resp:
            if resp.status not in (200, 204):
                text = await resp.text()
                raise RuntimeError(f"OTLP logs error {resp.status}: {text}")
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "chunk": encoded_chunk,
                                  "manifest": manifest_id}),
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
        raise KeyError(f"opentelemetry-log chunk not found: {token}")

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
                    location=json.dumps({"token": token, "chunk": chunk,
                                          "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            # Send empty payload -- reachable if 200 or 400
            async with session.post(
                f"{self._url}/v1/logs",
                data=b"",
                headers={"Content-Type": "application/json"},
            ) as resp:
                return resp.status in (200, 204, 400)
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
