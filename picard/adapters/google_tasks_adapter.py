"""Google Tasks adapter -- stores chunks as Google Tasks note fields.

Each write_chunk creates a task in a specified task list with the encoded
chunk as the notes field. Cold reads fetch via GET /tasks/v1/lists/{lid}/tasks/{tid}.

Requires: aiohttp, a Google OAuth2 access token and task list ID.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "google-tasks"
_BASE_URL = "https://tasks.googleapis.com/tasks/v1"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class GoogleTasksAdapter(Adapter):

    def __init__(self, *, access_token: str = "",
                 tasklist_id: str = "@default") -> None:
        self._access_token = access_token
        self._tasklist_id = tasklist_id
        self._reg: dict[str, str] = {}
        self._session = None

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            headers = {}
            if self._access_token:
                headers["Authorization"] = f"Bearer {self._access_token}"
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=8192,
            persistent=True,
            ordered=False,
            rate_limit_per_min=50,
            tags={"requires": "aiohttp,google-oauth-token", "substrate": "google-tasks-notes"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        payload = {
            "title": f"picard-{manifest_id[:16]}-{token}",
            "notes": encoded_chunk,
        }
        async with session.post(
                f"{_BASE_URL}/lists/{self._tasklist_id}/tasks",
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
                f"{_BASE_URL}/lists/{self._tasklist_id}/tasks/{loc['task_id']}") as resp:
            resp.raise_for_status()
            data = await resp.json()
        chunk = data["notes"]
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(
                f"{_BASE_URL}/lists/{self._tasklist_id}/tasks/{loc['task_id']}") as resp:
            resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        if not self._access_token or not self._tasklist_id:
            return []
        session = await self._get_session()
        prefix = f"picard-{manifest_id[:16]}-"
        refs = []
        page_token = None
        while True:
            params: dict = {"maxResults": "100", "showCompleted": "true",
                             "showHidden": "true"}
            if page_token:
                params["pageToken"] = page_token
            async with session.get(
                    f"{_BASE_URL}/lists/{self._tasklist_id}/tasks",
                    params=params) as resp:
                if resp.status != 200:
                    break
                data = await resp.json()
            for task in data.get("items", []):
                title = task.get("title", "")
                if title.startswith(prefix):
                    token = title[len(prefix):]
                    refs.append(StoredChunkRef(
                        adapter_name=_NAME,
                        location=json.dumps({"token": token,
                                              "task_id": task["id"],
                                              "manifest": manifest_id}),
                    ))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return refs

    async def health_check(self) -> bool:
        if not self._access_token:
            return False
        try:
            session = await self._get_session()
            async with session.get(f"{_BASE_URL}/users/@me/lists") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
