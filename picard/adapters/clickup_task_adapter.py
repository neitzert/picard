"""ClickUp task adapter -- stores chunks as ClickUp task descriptions.

Each write_chunk creates a task in a specified list with the encoded chunk
as the task description. Cold reads fetch via GET /api/v2/task/{task_id}.

Requires: aiohttp, a ClickUp personal API token and list ID.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "clickup-task"
_BASE_URL = "https://api.clickup.com/api/v2"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class ClickupTaskAdapter(Adapter):

    def __init__(self, *, api_token: str = "", list_id: str = "") -> None:
        self._api_token = api_token
        self._list_id = list_id
        self._reg: dict[str, str] = {}
        self._session = None

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            headers = {}
            if self._api_token:
                headers["Authorization"] = self._api_token
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            rate_limit_per_min=100,
            tags={"requires": "aiohttp,clickup-token", "substrate": "clickup-task-description"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        payload = {
            "name": f"picard-{manifest_id[:16]}-{token}",
            "description": encoded_chunk,
            "tags": [{"name": "picard"}],
        }
        async with session.post(
                f"{_BASE_URL}/list/{self._list_id}/task",
                json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
        task_id = data["id"]
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
                f"{_BASE_URL}/task/{loc['task_id']}") as resp:
            resp.raise_for_status()
            data = await resp.json()
        chunk = data["description"]
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(
                f"{_BASE_URL}/task/{loc['task_id']}") as resp:
            resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        if not self._api_token or not self._list_id:
            return []
        session = await self._get_session()
        prefix = f"picard-{manifest_id[:16]}-"
        refs = []
        page = 0
        while True:
            async with session.get(
                    f"{_BASE_URL}/list/{self._list_id}/task",
                    params={"page": str(page), "include_closed": "true"}) as resp:
                if resp.status != 200:
                    break
                data = await resp.json()
            tasks = data.get("tasks", [])
            for task in tasks:
                name = task.get("name", "")
                if name.startswith(prefix):
                    token = name[len(prefix):]
                    refs.append(StoredChunkRef(
                        adapter_name=_NAME,
                        location=json.dumps({"token": token,
                                              "task_id": task["id"],
                                              "manifest": manifest_id}),
                    ))
            if data.get("last_page"):
                break
            page += 1
        return refs

    async def health_check(self) -> bool:
        if not self._api_token or not self._list_id:
            return False
        try:
            session = await self._get_session()
            async with session.get(f"{_BASE_URL}/user") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
