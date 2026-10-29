"""IPFS adapter -- stores chunks via the Kubo IPFS HTTP API."""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "ipfs"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class IpfsAdapter(Adapter):

    def __init__(self, *, base_url: str = "http://127.0.0.1:5001") -> None:
        self._base_url = base_url.rstrip("/")
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
            max_chunk_bytes=10_000_000,
            persistent=True,
            ordered=False,
            rate_limit_per_min=100,
            tags={"requires": "aiohttp", "substrate": "ipfs-object"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        self._reg[token] = encoded_chunk
        cid: str | None = None
        session = await self._get_session()
        import aiohttp
        form = aiohttp.FormData()
        form.add_field("file", encoded_chunk.encode("utf-8"),
                       filename="chunk.txt",
                       content_type="text/plain")
        try:
            async with session.post(
                    f"{self._base_url}/api/v0/add",
                    data=form) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
            cid = data["Hash"]
        except Exception:
            pass
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "cid": cid,
                                 "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]
        cid = loc.get("cid")
        if not cid:
            raise KeyError(f"ipfs chunk has no CID: {token}")
        session = await self._get_session()
        async with session.post(
                f"{self._base_url}/api/v0/cat",
                params={"arg": cid}) as resp:
            resp.raise_for_status()
            chunk = await resp.text()
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        cid = loc.get("cid")
        if cid:
            session = await self._get_session()
            try:
                async with session.post(
                        f"{self._base_url}/api/v0/pin/rm",
                        params={"arg": cid}) as resp:
                    pass
            except Exception:
                pass
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token, "cid": None,
                                         "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.post(
                    f"{self._base_url}/api/v0/id") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
