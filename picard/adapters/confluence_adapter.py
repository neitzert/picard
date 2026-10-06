"""Confluence adapter — stores chunks as wiki pages.

Each chunk becomes a Confluence page in the configured space, with
the encoded chunk data in the page body (storage format).

Requires: pip install picard[confluence]  (aiohttp)
"""

from __future__ import annotations

import base64
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class ConfluenceAdapter(Adapter):
    """Scatter chunks into Confluence wiki pages."""

    def __init__(
        self,
        server_url: str,
        email: str,
        api_token: str,
        space_key: str,
        *,
        ancestor_id: str | None = None,
        max_chunk_bytes: int = 65536,
    ) -> None:
        self._server = server_url.rstrip("/")
        self._email = email
        self._token = api_token
        self._space = space_key
        self._ancestor = ancestor_id
        self._max = max_chunk_bytes
        self._client: object | None = None

    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        import aiohttp

        auth_str = base64.b64encode(f"{self._email}:{self._token}".encode()).decode()
        self._client = aiohttp.ClientSession(
            headers={
                "Authorization": f"Basic {auth_str}",
                "Content-Type": "application/json",
            },
        )
        return self._client

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="confluence",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=60,
        )

    def _page_title(self, encoded_chunk: str) -> str:
        try:
            chunk = Chunk.decode(encoded_chunk)
            return f"pchk-{chunk.manifest_id}-{chunk.block_index}-{chunk.chunk_index}"
        except ValueError:
            return f"pchk-{uuid.uuid4().hex}"

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import html

        client = await self._ensure_client()
        title = self._page_title(encoded_chunk)
        escaped = html.escape(encoded_chunk)
        body_value = f"<ac:structured-macro ac:name='code'><ac:plain-text-body><![CDATA[{encoded_chunk}]]></ac:plain-text-body></ac:structured-macro>"
        payload: dict = {
            "type": "page",
            "title": title,
            "space": {"key": self._space},
            "body": {
                "storage": {
                    "value": body_value,
                    "representation": "storage",
                }
            },
        }
        if self._ancestor:
            payload["ancestors"] = [{"id": self._ancestor}]
        async with client.post(
            f"{self._server}/wiki/rest/api/content",
            json=payload,
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        page_id = data["id"]
        loc = f"confluence://{self._space}/{page_id}"
        return StoredChunkRef(adapter_name="confluence", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import re

        client = await self._ensure_client()
        page_id = ref.location.rsplit("/", 1)[-1]
        async with client.get(
            f"{self._server}/wiki/rest/api/content/{page_id}",
            params={"expand": "body.storage"},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        body = data["body"]["storage"]["value"]
        match = re.search(r"<!\[CDATA\[(.+?)]]>", body, re.DOTALL)
        if match:
            return match.group(1)
        raise KeyError(f"chunk data not found in page: {page_id}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        client = await self._ensure_client()
        page_id = ref.location.rsplit("/", 1)[-1]
        try:
            async with client.delete(
                f"{self._server}/wiki/rest/api/content/{page_id}",
            ) as resp:
                return resp.status in (200, 204)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._ensure_client()
        cql = f'space = "{self._space}" AND title ~ "pchk-{manifest_id}-"'
        refs: list[StoredChunkRef] = []
        try:
            async with client.get(
                f"{self._server}/wiki/rest/api/content",
                params={"cql": cql, "limit": 1000},
            ) as resp:
                data = await resp.json()
            for page in data.get("results", []):
                loc = f"confluence://{self._space}/{page['id']}"
                refs.append(StoredChunkRef(adapter_name="confluence", location=loc))
        except Exception:
            pass
        return refs

    async def health_check(self) -> bool:
        try:
            client = await self._ensure_client()
            async with client.get(
                f"{self._server}/wiki/rest/api/space/{self._space}",
            ) as resp:
                return resp.status == 200
        except Exception:
            return False
