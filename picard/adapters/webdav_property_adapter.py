"""WebDAV dead-property adapter -- stores chunks as custom DEAD properties.

Each write_chunk uses PROPPATCH to set a custom XML property
(DAV namespace: net.picard) on a dedicated resource path. Cold reads use
PROPFIND to retrieve the property value.

Differs from webdav.py which stores chunks as file bodies: this adapter
hides data inside resource metadata properties.

Requires: pip install aiohttp
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "webdav-property"
_PROP_NS = "urn:net:picard:chunk:1"
_PROP_LOCAL = "chunk-data"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


def _proppatch_body(encoded_chunk: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<D:propertyupdate xmlns:D="DAV:" xmlns:P="urn:net:picard:chunk:1">'
        "<D:set><D:prop>"
        f"<P:chunk-data>{encoded_chunk}</P:chunk-data>"
        "</D:prop></D:set>"
        "</D:propertyupdate>"
    )


def _propfind_body() -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<D:propfind xmlns:D="DAV:" xmlns:P="urn:net:picard:chunk:1">'
        "<D:prop><P:chunk-data/></D:prop>"
        "</D:propfind>"
    )


class WebdavPropertyAdapter(Adapter):

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:8080/dav",
        username: str = "",
        password: str = "",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._session = None
        self._reg: dict[str, str] = {}

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            auth = None
            if self._username:
                auth = aiohttp.BasicAuth(self._username, self._password)
            self._session = aiohttp.ClientSession(auth=auth)
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            tags={"requires": "aiohttp", "substrate": "webdav-dead-property"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        resource_url = f"{self._base_url}/picard-{token}.txt"
        session = await self._get_session()

        # Create the resource first
        async with session.put(resource_url, data=b" ") as resp:
            if resp.status not in (200, 201, 204):
                resp.raise_for_status()

        # Set the property
        async with session.request(
            "PROPPATCH",
            resource_url,
            data=_proppatch_body(encoded_chunk).encode(),
            headers={"Content-Type": "application/xml; charset=utf-8"},
        ) as resp:
            resp.raise_for_status()

        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "url": resource_url,
                                 "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]

        session = await self._get_session()
        async with session.request(
            "PROPFIND",
            loc["url"],
            data=_propfind_body().encode(),
            headers={"Content-Type": "application/xml; charset=utf-8",
                     "Depth": "0"},
        ) as resp:
            resp.raise_for_status()
            body = await resp.text()

        # Parse <P:chunk-data>...</P:chunk-data> out of the multistatus XML
        tag = "chunk-data>"
        start = body.find(tag)
        if start == -1:
            raise KeyError(f"webdav-property chunk not found: {token}")
        start += len(tag)
        end = body.find("<", start)
        chunk = body[start:end].strip()
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        token = loc["token"]
        self._reg.pop(token, None)
        try:
            session = await self._get_session()
            async with session.delete(loc["url"]) as resp:
                return resp.status in (200, 204, 404)
        except Exception:
            return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({
                        "token": token,
                        "url": f"{self._base_url}/picard-{token}.txt",
                        "manifest": manifest_id,
                    }),
                ))
        return refs

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.request("OPTIONS", self._base_url) as resp:
                return resp.status in (200, 204)
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
