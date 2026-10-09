"""Jira adapter — stores chunks as issue descriptions.

Each chunk becomes a Jira issue in the configured project, with
the encoded chunk data in the description field. Issue summaries
contain a picard marker for listing.

Requires: pip install picard[jira]  (aiohttp)
"""

from __future__ import annotations

import base64
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class JiraAdapter(Adapter):
    """Scatter chunks into Jira issue descriptions."""

    def __init__(
        self,
        server_url: str,
        email: str,
        api_token: str,
        project_key: str,
        *,
        issue_type: str = "Task",
        max_chunk_bytes: int = 32768,
    ) -> None:
        self._server = server_url.rstrip("/")
        self._email = email
        self._token = api_token
        self._project = project_key
        self._issue_type = issue_type
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
            name="jira",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=60,
        )

    def _summary(self, encoded_chunk: str) -> str:
        try:
            chunk = Chunk.decode(encoded_chunk)
            return f"[picard:{chunk.manifest_id}:{chunk.block_index}:{chunk.chunk_index}]"
        except ValueError:
            return f"[picard:{uuid.uuid4().hex}]"

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        client = await self._ensure_client()
        summary = self._summary(encoded_chunk)
        payload = {
            "fields": {
                "project": {"key": self._project},
                "summary": summary,
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "codeBlock",
                            "content": [{"type": "text", "text": encoded_chunk}],
                        }
                    ],
                },
                "issuetype": {"name": self._issue_type},
            }
        }
        async with client.post(
            f"{self._server}/rest/api/3/issue",
            json=payload,
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        issue_key = data["key"]
        loc = f"jira://{self._server}/{issue_key}"
        return StoredChunkRef(adapter_name="jira", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        client = await self._ensure_client()
        issue_key = ref.location.rsplit("/", 1)[-1]
        async with client.get(
            f"{self._server}/rest/api/3/issue/{issue_key}",
            params={"fields": "description"},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        desc = data["fields"]["description"]
        for block in desc.get("content", []):
            if block.get("type") == "codeBlock":
                for item in block.get("content", []):
                    if item.get("type") == "text":
                        return item["text"]
        raise KeyError(f"chunk data not found in issue: {issue_key}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        client = await self._ensure_client()
        issue_key = ref.location.rsplit("/", 1)[-1]
        try:
            async with client.delete(
                f"{self._server}/rest/api/3/issue/{issue_key}",
            ) as resp:
                return resp.status in (200, 204)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._ensure_client()
        jql = f'project = {self._project} AND summary ~ "[picard:{manifest_id}:"'
        refs: list[StoredChunkRef] = []
        try:
            async with client.get(
                f"{self._server}/rest/api/3/search",
                params={"jql": jql, "fields": "key", "maxResults": 1000},
            ) as resp:
                data = await resp.json()
            for issue in data.get("issues", []):
                loc = f"jira://{self._server}/{issue['key']}"
                refs.append(StoredChunkRef(adapter_name="jira", location=loc))
        except Exception:
            pass
        return refs

    async def health_check(self) -> bool:
        try:
            client = await self._ensure_client()
            async with client.get(f"{self._server}/rest/api/3/myself") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
