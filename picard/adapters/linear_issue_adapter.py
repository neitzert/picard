"""Linear issue adapter -- stores chunks as Linear issue descriptions.

Each write_chunk creates a Linear issue with the encoded chunk as its
description via the Linear GraphQL API. Cold reads query the issue by ID.

Requires: aiohttp, a Linear API key (https://linear.app/settings/api).
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "linear-issue"
_API_URL = "https://api.linear.app/graphql"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class LinearIssueAdapter(Adapter):

    def __init__(self, *, api_key: str = "", team_id: str = "") -> None:
        self._api_key = api_key
        self._team_id = team_id
        self._reg: dict[str, str] = {}
        self._session = None

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            headers = {"Content-Type": "application/json"}
            if self._api_key:
                headers["Authorization"] = self._api_key
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            rate_limit_per_min=60,
            tags={"requires": "aiohttp,linear-api-key", "substrate": "linear-issue-description"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        title = f"picard-{token}"
        mutation = """
        mutation CreateIssue($title: String!, $description: String!, $teamId: String!) {
          issueCreate(input: {title: $title, description: $description, teamId: $teamId}) {
            success
            issue { id identifier }
          }
        }
        """
        session = await self._get_session()
        async with session.post(_API_URL, json={
            "query": mutation,
            "variables": {"title": title, "description": encoded_chunk,
                          "teamId": self._team_id},
        }) as resp:
            resp.raise_for_status()
            data = await resp.json()
        issue = data["data"]["issueCreate"]["issue"]
        issue_id = issue["id"]
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "issue_id": issue_id,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        query = """
        query GetIssue($id: String!) {
          issue(id: $id) { description }
        }
        """
        session = await self._get_session()
        async with session.post(_API_URL, json={
            "query": query,
            "variables": {"id": loc["issue_id"]},
        }) as resp:
            resp.raise_for_status()
            data = await resp.json()
        chunk = data["data"]["issue"]["description"]
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        mutation = """
        mutation DeleteIssue($id: String!) {
          issueDelete(id: $id) { success }
        }
        """
        session = await self._get_session()
        async with session.post(_API_URL, json={
            "query": mutation,
            "variables": {"id": loc["issue_id"]},
        }) as resp:
            resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token, "issue_id": "",
                                          "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        if not self._api_key or not self._team_id:
            return False
        try:
            session = await self._get_session()
            async with session.post(_API_URL, json={
                "query": "query { viewer { id } }",
            }) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
