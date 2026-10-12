"""CouchDB adapter — stores chunks as CouchDB documents.

Each chunk becomes a document in a dedicated database, keyed by
manifest/block/chunk identity.

Requires: pip install picard[couchdb]  (just httpx)
"""

from __future__ import annotations

from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class CouchDbAdapter(Adapter):
    """Scatter chunks into CouchDB documents."""

    def __init__(
        self,
        *,
        url: str = "http://localhost:5984",
        database: str = "picard",
        username: str | None = None,
        password: str | None = None,
        max_chunk_bytes: int = 5_000_000,
    ) -> None:
        self._url = url.rstrip("/")
        self._database = database
        self._username = username
        self._password = password
        self._max = max_chunk_bytes
        self._client: object | None = None
        self._db_ensured = False

    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        import httpx

        auth = (self._username, self._password) if self._username else None
        self._client = httpx.AsyncClient(auth=auth, timeout=30.0)
        return self._client

    async def _ensure_db(self):
        if self._db_ensured:
            return
        client = await self._ensure_client()
        resp = await client.put(f"{self._url}/{self._database}")
        if resp.status_code not in (201, 412):
            resp.raise_for_status()
        self._db_ensured = True

    def _doc_id(self, manifest_id: str, block: int, chunk_index: int) -> str:
        return f"pchk_{manifest_id}_b{block}_c{chunk_index}"

    @property
    def _db_url(self) -> str:
        return f"{self._url}/{self._database}"

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="couchdb",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=500,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        await self._ensure_db()
        client = await self._ensure_client()
        chunk = Chunk.decode(encoded_chunk)
        doc_id = self._doc_id(
            chunk.manifest_id, chunk.block_index, chunk.chunk_index
        )
        resp = await client.put(
            f"{self._db_url}/{doc_id}",
            json={"data": encoded_chunk},
        )
        resp.raise_for_status()
        return StoredChunkRef(adapter_name="couchdb", location=doc_id)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        client = await self._ensure_client()
        resp = await client.get(f"{self._db_url}/{ref.location}")
        resp.raise_for_status()
        return resp.json()["data"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        client = await self._ensure_client()
        try:
            resp = await client.get(f"{self._db_url}/{ref.location}")
            if resp.status_code == 404:
                return False
            rev = resp.json()["_rev"]
            del_resp = await client.delete(
                f"{self._db_url}/{ref.location}",
                params={"rev": rev},
            )
            return del_resp.status_code == 200
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._ensure_client()
        prefix = f"pchk_{manifest_id}_"
        resp = await client.get(
            f"{self._db_url}/_all_docs",
            params={
                "startkey": f'"{prefix}"',
                "endkey": f'"{prefix}￰"',
                "include_docs": "false",
            },
        )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        rows = resp.json().get("rows", [])
        return [
            StoredChunkRef(adapter_name="couchdb", location=row["id"])
            for row in rows
        ]

    async def health_check(self) -> bool:
        try:
            client = await self._ensure_client()
            resp = await client.get(f"{self._url}/")
            return resp.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
