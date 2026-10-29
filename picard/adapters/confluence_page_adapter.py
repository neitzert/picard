"""Confluence page adapter -- stores chunks as Confluence Cloud wiki pages.

Each write_chunk creates a Confluence page in a configured space with the
encoded chunk as the page body (storage format).

Requires: aiohttp, Confluence Cloud base URL, user email, API token, space key.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "confluence-page"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class ConfluencePageAdapter(Adapter):

    def __init__(self, *, base_url: str = "", email: str = "",
                 api_token: str = "", space_key: str = "PICARD") -> None:
        self._base = base_url.rstrip("/")
        self._email = email
        self._api_token = api_token
        self._space_key = space_key
        self._session = None
        self._reg: dict[str, str] = {}

    async def _get_session(self):
        if self._session is None:
            import aiohttp, base64
            creds = base64.b64encode(
                f"{self._email}:{self._api_token}".encode()
            ).decode()
            self._session = aiohttp.ClientSession(headers={
                "Authorization": f"Basic {creds}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            })
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            rate_limit_per_min=100,
            tags={"requires": "confluence-cloud-credentials,space-key",
                  "substrate": "confluence-page-body"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        payload = {
            "type": "page",
            "title": f"picard:{manifest_id[:16]}:{token}",
            "space": {"key": self._space_key},
            "body": {"storage": {
                "value": f"<p>{encoded_chunk}</p>",
                "representation": "storage",
            }},
        }
        async with session.post(f"{self._base}/wiki/rest/api/content",
                                json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
        page_id = data["id"]
        self._reg[token] = encoded_chunk
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token,
                                                    "page_id": page_id,
                                                    "manifest": manifest_id}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        async with session.get(
                f"{self._base}/wiki/rest/api/content/{loc['page_id']}",
                params={"expand": "body.storage"}) as resp:
            resp.raise_for_status()
            data = await resp.json()
        import re
        html = data["body"]["storage"]["value"]
        chunk = re.sub(r"<[^>]+>", "", html).strip()
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(
                f"{self._base}/wiki/rest/api/content/{loc['page_id']}") as resp:
            return resp.status in (204, 404)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        if not self._base or not self._email or not self._api_token:
            return []
        session = await self._get_session()
        import urllib.parse
        prefix = f"picard:{manifest_id[:16]}:"
        cql = urllib.parse.quote(
            f'space="{self._space_key}" AND title~"{prefix}" AND type=page')
        refs = []
        start = 0
        while True:
            async with session.get(
                    f"{self._base}/wiki/rest/api/content/search",
                    params={"cql": cql, "limit": "50",
                            "start": str(start)}) as resp:
                if resp.status != 200:
                    break
                data = await resp.json()
            for page in data.get("results", []):
                title = page.get("title", "")
                if title.startswith(prefix):
                    token = title[len(prefix):]
                    refs.append(StoredChunkRef(
                        adapter_name=_NAME,
                        location=json.dumps({"token": token,
                                              "page_id": page["id"],
                                              "manifest": manifest_id}),
                    ))
            size = data.get("size", 0)
            if size < 50:
                break
            start += size
        return refs

    async def health_check(self) -> bool:
        if not self._base or not self._email or not self._api_token:
            return False
        try:
            session = await self._get_session()
            async with session.get(
                    f"{self._base}/wiki/rest/api/user/current") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
