"""GitLab snippet adapter -- stores chunks as GitLab personal snippets.

Each write_chunk creates a private GitLab snippet with the encoded chunk
as the file content. Supports both GitLab.com and self-hosted instances.

Requires: aiohttp, GitLab personal access token.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "gitlab-snippet"
_DEFAULT_BASE = "https://gitlab.com"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class GitlabSnippetAdapter(Adapter):

    def __init__(self, *, token: str = "",
                 base_url: str = _DEFAULT_BASE) -> None:
        self._token = token
        self._base = base_url.rstrip("/")
        self._session = None
        self._reg: dict[str, str] = {}

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession(headers={
                "PRIVATE-TOKEN": self._token,
                "Content-Type": "application/json",
            })
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            rate_limit_per_min=60,
            tags={"requires": "gitlab-personal-access-token",
                  "substrate": "gitlab-snippet"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        payload = {
            "title": f"picard:{manifest_id}:{token}",
            "visibility": "private",
            "files": [{"file_path": "chunk.txt", "content": encoded_chunk}],
        }
        async with session.post(f"{self._base}/api/v4/snippets",
                                json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
        snippet_id = data["id"]
        self._reg[token] = encoded_chunk
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token,
                                                    "snippet_id": snippet_id,
                                                    "manifest": manifest_id}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        async with session.get(
                f"{self._base}/api/v4/snippets/{loc['snippet_id']}/raw") as resp:
            resp.raise_for_status()
            chunk = await resp.text()
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(
                f"{self._base}/api/v4/snippets/{loc['snippet_id']}") as resp:
            return resp.status in (204, 404)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        seen: set[str] = set()
        try:
            session = await self._get_session()
            page = 1
            while True:
                async with session.get(
                        f"{self._base}/api/v4/snippets",
                        params={"per_page": "100", "page": str(page)}) as resp:
                    if resp.status != 200:
                        break
                    items = await resp.json()
                if not items:
                    break
                for item in items:
                    title = item.get("title", "")
                    prefix = f"picard:{manifest_id}:"
                    if title.startswith(prefix):
                        tok = title[len(prefix):]
                        snippet_id = item["id"]
                        if tok not in seen:
                            seen.add(tok)
                            refs.append(StoredChunkRef(
                                adapter_name=_NAME,
                                location=json.dumps({"token": tok,
                                                      "snippet_id": snippet_id,
                                                      "manifest": manifest_id}),
                            ))
                page += 1
        except Exception:
            pass
        for tok, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id and tok not in seen:
                seen.add(tok)
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": tok, "snippet_id": "",
                                          "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        if not self._token:
            return False
        try:
            session = await self._get_session()
            async with session.get(f"{self._base}/api/v4/user") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
