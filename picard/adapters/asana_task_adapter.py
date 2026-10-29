"""Asana task adapter -- stores chunks as Asana task notes.

Each write_chunk creates a task in a specified project with the encoded
chunk as the task notes field. Cold reads fetch via GET /api/1.0/tasks/{id}.

Requires: aiohttp, an Asana personal access token and project GID.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "asana-task"
_BASE_URL = "https://app.asana.com/api/1.0"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class AsanaTaskAdapter(Adapter):

    def __init__(self, *, access_token: str = "",
                 project_id: str = "") -> None:
        self._access_token = access_token
        self._project_id = project_id
        self._reg: dict[str, str] = {}
        self._session = None

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            headers = {"Accept": "application/json"}
            if self._access_token:
                headers["Authorization"] = f"Bearer {self._access_token}"
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            rate_limit_per_min=150,
            tags={"requires": "aiohttp,asana-token", "substrate": "asana-task-notes"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        payload = {
            "data": {
                "name": f"picard-{token}",
                "notes": encoded_chunk,
                "projects": [self._project_id],
            }
        }
        async with session.post(f"{_BASE_URL}/tasks", json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
        task_id = data["data"]["gid"]
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "task_id": task_id,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        async with session.get(
                f"{_BASE_URL}/tasks/{loc['task_id']}") as resp:
            resp.raise_for_status()
            data = await resp.json()
        chunk = data["data"]["notes"]
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(
                f"{_BASE_URL}/tasks/{loc['task_id']}") as resp:
            resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token, "task_id": "",
                                          "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        if not self._access_token or not self._project_id:
            return False
        try:
            session = await self._get_session()
            async with session.get(f"{_BASE_URL}/users/me") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
