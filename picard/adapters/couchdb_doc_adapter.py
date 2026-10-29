"""CouchDB document adapter -- stores chunks as CouchDB JSON documents.

Each write_chunk creates a document with _id=picard-{token} in a dedicated
database. Cold reads fetch the document by _id. Uses the CouchDB HTTP API.

Requires: pip install aiohttp
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "couchdb-doc"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class CouchdbDocAdapter(Adapter):

    def __init__(
        self,
        *,
        url: str = "http://127.0.0.1:5984",
        database: str = "picard",
        username: str = "admin",
        password: str = "",
    ) -> None:
        self._base_url = url.rstrip("/")
        self._db = database
        self._username = username
        self._password = password
        self._session = None
        self._revs: dict[str, str] = {}
        self._reg: dict[str, str] = {}

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            auth = aiohttp.BasicAuth(self._username, self._password) if self._username else None
            self._session = aiohttp.ClientSession(auth=auth)
        return self._session

    async def _ensure_db(self) -> None:
        session = await self._get_session()
        async with session.put(f"{self._base_url}/{self._db}") as resp:
            pass  # 201 = created, 412 = already exists

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=1_048_576,
            persistent=True,
            ordered=False,
            tags={"requires": "aiohttp", "substrate": "couchdb-document"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        await self._ensure_db()
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        doc_id = f"picard-{token}"

        session = await self._get_session()
        async with session.put(
            f"{self._base_url}/{self._db}/{doc_id}",
            json={"chunk": encoded_chunk, "manifest": manifest_id,
                  "token": token},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            self._revs[token] = data.get("rev", "")

        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "doc_id": doc_id,
                                 "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]

        session = await self._get_session()
        async with session.get(
            f"{self._base_url}/{self._db}/{loc['doc_id']}"
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        chunk = data.get("chunk", "")
        if not chunk:
            raise KeyError(f"couchdb-doc chunk not found: {token}")
        self._revs[token] = data.get("_rev", "")
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        token = loc["token"]
        doc_id = loc.get("doc_id", f"picard-{token}")
        self._reg.pop(token, None)

        session = await self._get_session()
        rev = self._revs.get(token)
        if not rev:
            async with session.get(
                f"{self._base_url}/{self._db}/{doc_id}"
            ) as r:
                if r.status == 200:
                    d = await r.json()
                    rev = d.get("_rev", "")

        if rev:
            async with session.delete(
                f"{self._base_url}/{self._db}/{doc_id}",
                params={"rev": rev},
            ) as resp:
                return resp.status in (200, 404)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token,
                                         "doc_id": f"picard-{token}",
                                         "manifest": manifest_id}),
                ))
        try:
            session = await self._get_session()
            async with session.post(
                f"{self._base_url}/{self._db}/_find",
                json={"selector": {"manifest": manifest_id}, "fields": ["token"]},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    tokens_seen = {json.loads(r.location)["token"] for r in refs}
                    for doc in data.get("docs", []):
                        tok = doc.get("token", "")
                        if tok and tok not in tokens_seen:
                            refs.append(StoredChunkRef(
                                adapter_name=_NAME,
                                location=json.dumps({"token": tok,
                                                     "doc_id": f"picard-{tok}",
                                                     "manifest": manifest_id}),
                            ))
        except Exception:
            pass
        return refs

    async def health_check(self) -> bool:
        if not self._username:
            return False
        try:
            session = await self._get_session()
            async with session.get(self._base_url) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        self._revs.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
