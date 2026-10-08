"""HTTP transport — expose local adapters to remote PICARD peers.

Runs as a FastAPI sub-app, mountable on the existing web dashboard
or as a standalone server via ``picard serve``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Request
from pydantic import BaseModel

from picard.adapters.base import Adapter, StoredChunkRef

transport_app = FastAPI(title="PICARD Transport", docs_url=None, redoc_url=None)

_adapters: dict[str, Adapter] = {}
_secret: str = ""


class WriteRequest(BaseModel):
    adapter: str
    data: str


class ReadRequest(BaseModel):
    adapter: str
    ref: str


class DeleteRequest(BaseModel):
    adapter: str
    ref: str


class ListRequest(BaseModel):
    adapter: str
    manifest_id: str


def configure(adapters: dict[str, Adapter], secret: str = "") -> None:
    global _adapters, _secret
    _adapters = adapters
    _secret = secret


_HMAC_MAX_AGE = 300

def _verify_hmac(
    request_body: bytes,
    signature: str | None,
    timestamp: str | None = None,
    path: str = "",
) -> bool:
    if not _secret:
        return True
    if not signature:
        return False
    if timestamp:
        try:
            ts = float(timestamp)
            if abs(time.time() - ts) > _HMAC_MAX_AGE:
                return False
        except (ValueError, TypeError):
            return False
    msg = path.encode() + b"\n"
    if timestamp:
        msg += timestamp.encode() + b"\n"
    msg += request_body
    expected = hmac.new(
        _secret.encode(), msg, hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _get_adapter(name: str) -> Adapter:
    adapter = _adapters.get(name)
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"adapter {name!r} not found")
    return adapter


@transport_app.post("/transport/v1/write_chunk")
async def write_chunk(
    req: WriteRequest,
    request: Request,
    x_signature: Optional[str] = Header(None),
    x_timestamp: Optional[str] = Header(None),
):
    body = await request.body()
    if not _verify_hmac(body, x_signature, x_timestamp, "/transport/v1/write_chunk"):
        raise HTTPException(status_code=403, detail="invalid signature")
    adapter = _get_adapter(req.adapter)
    ref = await adapter.write_chunk(req.data)
    return {"adapter": ref.adapter_name, "location": ref.location}


@transport_app.post("/transport/v1/read_chunk")
async def read_chunk(
    req: ReadRequest,
    request: Request,
    x_signature: Optional[str] = Header(None),
    x_timestamp: Optional[str] = Header(None),
):
    body = await request.body()
    if not _verify_hmac(body, x_signature, x_timestamp, "/transport/v1/read_chunk"):
        raise HTTPException(status_code=403, detail="invalid signature")
    adapter = _get_adapter(req.adapter)
    ref = StoredChunkRef(adapter_name=req.adapter, location=req.ref)
    data = await adapter.read_chunk(ref)
    return {"data": data}


@transport_app.post("/transport/v1/delete_chunk")
async def delete_chunk(
    req: DeleteRequest,
    request: Request,
    x_signature: Optional[str] = Header(None),
    x_timestamp: Optional[str] = Header(None),
):
    body = await request.body()
    if not _verify_hmac(body, x_signature, x_timestamp, "/transport/v1/delete_chunk"):
        raise HTTPException(status_code=403, detail="invalid signature")
    adapter = _get_adapter(req.adapter)
    ref = StoredChunkRef(adapter_name=req.adapter, location=req.ref)
    deleted = await adapter.delete_chunk(ref)
    return {"deleted": deleted}


@transport_app.post("/transport/v1/list_chunks")
async def list_chunks(
    req: ListRequest,
    request: Request,
    x_signature: Optional[str] = Header(None),
    x_timestamp: Optional[str] = Header(None),
):
    body = await request.body()
    if not _verify_hmac(body, x_signature, x_timestamp, "/transport/v1/list_chunks"):
        raise HTTPException(status_code=403, detail="invalid signature")
    adapter = _get_adapter(req.adapter)
    refs = await adapter.list_chunks(req.manifest_id)
    return {
        "refs": [
            {"adapter": r.adapter_name, "location": r.location} for r in refs
        ]
    }


@transport_app.get("/transport/v1/adapters")
async def list_adapters():
    results = []
    for name, adapter in _adapters.items():
        p = adapter.profile()
        results.append({
            "name": name,
            "max_chunk_bytes": p.max_chunk_bytes,
            "persistent": p.persistent,
            "tags": p.tags,
        })
    return {"adapters": results}


@transport_app.get("/transport/v1/health")
async def health():
    capacities: dict[str, dict | None] = {}
    for name, adapter in _adapters.items():
        try:
            capacities[name] = await adapter.capacity()
        except Exception:
            capacities[name] = None
    return {
        "status": "ok",
        "adapters": len(_adapters),
        "capacities": capacities,
        "timestamp": time.time(),
    }
