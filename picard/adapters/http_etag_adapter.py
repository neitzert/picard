"""HTTP ETag adapter -- stores chunks in HTTP response ETag header values.

Each write_chunk builds a synthetic HTTP/1.1 200 response with an ETag
header carrying the encoded chunk. The response bytes are held in-process.
Cold reads parse the ETag. No network required. Stdlib only.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "http_etag"

_ETAG_RE = re.compile(r'^ETag: "([^"]+)"', re.MULTILINE)
_DATA_RE = re.compile(r"^X-Picard-Data: ([^\r\n]+)", re.MULTILINE)


def _make_response(token: str, encoded_chunk: str) -> str:
    ehash = hashlib.sha256(encoded_chunk.encode()).hexdigest()[:16]
    etag_value = f"{ehash}-{token}"
    return (
        "HTTP/1.1 200 OK\r\n"
        f"ETag: \"{etag_value}\"\r\n"
        f"X-Picard-Data: {encoded_chunk}\r\n"
        "Content-Length: 0\r\n"
        "\r\n"
    )


def _parse_response(response: str) -> tuple[str, str]:
    """Return (etag_value, encoded_chunk) from response."""
    m = _ETAG_RE.search(response)
    if not m:
        raise KeyError("ETag header not found in HTTP response")
    etag_value = m.group(1)
    data_m = _DATA_RE.search(response)
    if not data_m:
        raise KeyError("X-Picard-Data header not found")
    return etag_value, data_m.group(1).strip()


class HttpEtagAdapter(Adapter):

    def __init__(self) -> None:
        self._reg: dict[str, tuple[str, str]] = {}  # token -> (response, encoded_chunk)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=False,
            ordered=False,
            tags={"requires": "stdlib", "substrate": "http-etag-header"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            token = uuid.uuid4().hex[:16]
            response = _make_response(token, encoded_chunk)
            return token, response

        token, response = await asyncio.to_thread(_write)
        self._reg[token] = (response, encoded_chunk)
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = json.loads(ref.location)["token"]
        if token not in self._reg:
            raise KeyError(f"http_etag chunk not in registry: {token}")
        response, _ = self._reg[token]
        _, encoded_chunk = _parse_response(response)
        return encoded_chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = json.loads(ref.location)["token"]
        self._reg.pop(token, None)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        from picard.chunker import Chunk
        refs = []
        for token, (_, data) in self._reg.items():
            try:
                if Chunk.decode(data).manifest_id == manifest_id:
                    refs.append(StoredChunkRef(adapter_name=_NAME,
                                               location=json.dumps({"token": token})))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            resp = _make_response("deadbeef01234567", "test")
            _, chunk = _parse_response(resp)
            return chunk == "test"
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
