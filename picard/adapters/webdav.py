"""WebDAV adapter — stores chunks as files on a WebDAV server.

Each chunk becomes a file under a manifest-specific collection (directory).
Uses standard WebDAV methods: PUT, GET, DELETE, MKCOL, PROPFIND.

Requires: pip install httpx
"""

from __future__ import annotations

import re
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_HREF_RE = re.compile(r"<D:href>([^<]+)</D:href>", re.IGNORECASE)


class WebdavAdapter(Adapter):
    """Scatter chunks into files on a WebDAV server."""

    def __init__(
        self,
        url: str,
        username: str | None = None,
        password: str | None = None,
        base_path: str = "/picard/",
        *,
        max_chunk_bytes: int = 50_000_000,
    ) -> None:
        self._url = url.rstrip("/")
        self._username = username
        self._password = password
        self._base_path = base_path if base_path.endswith("/") else base_path + "/"
        self._max = max_chunk_bytes
        self._client: object | None = None

    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        import httpx

        auth = (self._username, self._password) if self._username else None
        self._client = httpx.AsyncClient(auth=auth, timeout=30.0)
        return self._client

    def _full_url(self, rel_path: str = "") -> str:
        return f"{self._url}{self._base_path}{rel_path}"

    async def _ensure_collection(self, path: str) -> None:
        client = await self._ensure_client()
        resp = await client.request("MKCOL", path)
        # 201 = created, 405 = already exists, 301 = redirect — all fine
        if resp.status_code not in (201, 405, 301):
            resp.raise_for_status()

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="webdav",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=120,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        client = await self._ensure_client()
        chunk = Chunk.decode(encoded_chunk)

        manifest_dir = f"{chunk.manifest_id}/"
        filename = f"b{chunk.block_index}_c{chunk.chunk_index}.pchk"
        rel_path = f"{manifest_dir}{filename}"

        await self._ensure_collection(self._full_url(manifest_dir))

        await client.put(
            self._full_url(rel_path),
            content=encoded_chunk,
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )
        return StoredChunkRef(adapter_name="webdav", location=rel_path)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        client = await self._ensure_client()
        resp = await client.get(self._full_url(ref.location))
        resp.raise_for_status()
        return resp.text

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        client = await self._ensure_client()
        try:
            resp = await client.request("DELETE", self._full_url(ref.location))
            return resp.status_code in (200, 204)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._ensure_client()
        col_url = self._full_url(f"{manifest_id}/")

        resp = await client.request(
            "PROPFIND",
            col_url,
            headers={"Depth": "1", "Content-Type": "application/xml"},
            content=(
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<D:propfind xmlns:D="DAV:">'
                "<D:prop><D:resourcetype/></D:prop>"
                "</D:propfind>"
            ),
        )

        if resp.status_code != 207:
            return []

        refs: list[StoredChunkRef] = []
        for href_match in _HREF_RE.finditer(resp.text):
            href = href_match.group(1)
            if href.endswith(".pchk"):
                # Extract relative path: manifest_id/bN_cN.pchk
                parts = href.rstrip("/").rsplit("/", 2)
                if len(parts) >= 2:
                    rel_path = f"{parts[-2]}/{parts[-1]}"
                    refs.append(
                        StoredChunkRef(
                            adapter_name="webdav", location=rel_path
                        )
                    )
        return refs

    async def health_check(self) -> bool:
        try:
            client = await self._ensure_client()
            resp = await client.request(
                "PROPFIND",
                self._full_url(),
                headers={"Depth": "0"},
            )
            return resp.status_code == 207
        except Exception:
            return False

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
