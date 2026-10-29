"""GitHub Gist adapter -- stores chunks as private GitHub Gist file contents."""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "github-gist"
_FILE_KEY = "chunk.txt"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class GithubGistAdapter(Adapter):

    def __init__(self, *, token: str = "",
                 base_url: str = "https://api.github.com") -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._session = None
        self._reg: dict[str, str] = {}

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json",
             "X-GitHub-Api-Version": "2022-11-28"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession(headers=self._headers())
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
            tags={"requires": "aiohttp", "substrate": "github-gist"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        payload = {
            "description": f"picard:{token}",
            "public": False,
            "files": {_FILE_KEY: {"content": encoded_chunk}},
        }
        async with session.post(f"{self._base_url}/gists",
                                json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
        gist_id = data["id"]
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "gist_id": gist_id,
                                 "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]
        session = await self._get_session()
        async with session.get(f"{self._base_url}/gists/{loc['gist_id']}") as resp:
            resp.raise_for_status()
            data = await resp.json()
        chunk = data["files"][_FILE_KEY]["content"]
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(
                f"{self._base_url}/gists/{loc['gist_id']}") as resp:
            return resp.status in (204, 404)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        session = await self._get_session()
        refs: list[StoredChunkRef] = []
        page = 1
        while True:
            async with session.get(
                    f"{self._base_url}/gists",
                    params={"per_page": 100, "page": page}) as resp:
                resp.raise_for_status()
                items = await resp.json()
            if not items:
                break
            for item in items:
                desc = item.get("description", "")
                if not desc.startswith("picard:"):
                    continue
                token = desc[len("picard:"):]
                gist_id = item["id"]
                chunk = self._reg.get(token, "")
                if not chunk:
                    try:
                        async with session.get(
                                f"{self._base_url}/gists/{gist_id}") as r:
                            r.raise_for_status()
                            d = await r.json()
                        chunk = d["files"][_FILE_KEY]["content"]
                    except Exception:
                        continue
                if _manifest_id_from_chunk(chunk) == manifest_id:
                    refs.append(StoredChunkRef(
                        adapter_name=_NAME,
                        location=json.dumps({"token": token,
                                             "gist_id": gist_id,
                                             "manifest": manifest_id}),
                    ))
            if len(items) < 100:
                break
            page += 1
        return refs

    async def health_check(self) -> bool:
        if not self._token:
            return False
        try:
            session = await self._get_session()
            async with session.get(f"{self._base_url}/user") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
