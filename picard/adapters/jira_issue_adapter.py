"""Jira issue adapter -- stores chunks as Jira issue descriptions.

Each write_chunk creates a Jira issue with the encoded chunk in the
description field via the Jira Cloud REST API v3.

Requires: aiohttp, Jira base URL, user email, API token, project key.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "jira-issue"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class JiraIssueAdapter(Adapter):

    def __init__(self, *, base_url: str = "", email: str = "",
                 api_token: str = "", project_key: str = "PICARD") -> None:
        self._base = base_url.rstrip("/")
        self._email = email
        self._api_token = api_token
        self._project_key = project_key
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
            max_chunk_bytes=32768,
            persistent=True,
            ordered=False,
            rate_limit_per_min=100,
            tags={"requires": "jira-cloud-credentials",
                  "substrate": "jira-issue-description"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        payload = {
            "fields": {
                "project": {"key": self._project_key},
                "summary": f"picard:{token}",
                "issuetype": {"name": "Task"},
                "description": {
                    "type": "doc", "version": 1,
                    "content": [{"type": "paragraph",
                                 "content": [{"type": "text",
                                              "text": encoded_chunk}]}],
                },
            }
        }
        async with session.post(f"{self._base}/rest/api/3/issue",
                                json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
        issue_key = data["key"]
        self._reg[token] = encoded_chunk
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token,
                                                    "issue_key": issue_key,
                                                    "manifest": manifest_id}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        async with session.get(
                f"{self._base}/rest/api/3/issue/{loc['issue_key']}") as resp:
            resp.raise_for_status()
            data = await resp.json()
        content = data["fields"]["description"]["content"]
        chunk = content[0]["content"][0]["text"]
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(
                f"{self._base}/rest/api/3/issue/{loc['issue_key']}") as resp:
            return resp.status in (204, 404)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        seen: set[str] = set()
        try:
            session = await self._get_session()
            jql = f'project = "{self._project_key}" AND summary ~ "picard:"'
            start = 0
            while True:
                async with session.get(
                        f"{self._base}/rest/api/3/search",
                        params={"jql": jql, "fields": "summary",
                                "startAt": str(start), "maxResults": "100"}) as resp:
                    if resp.status != 200:
                        break
                    data = await resp.json()
                issues = data.get("issues", [])
                if not issues:
                    break
                for issue in issues:
                    summary = issue.get("fields", {}).get("summary", "")
                    if summary.startswith("picard:"):
                        tok = summary[len("picard:"):]
                        issue_key = issue["key"]
                        if tok not in seen:
                            seen.add(tok)
                            refs.append(StoredChunkRef(
                                adapter_name=_NAME,
                                location=json.dumps({"token": tok,
                                                      "issue_key": issue_key,
                                                      "manifest": manifest_id}),
                            ))
                start += len(issues)
                if start >= data.get("total", 0):
                    break
        except Exception:
            pass
        for tok, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id and tok not in seen:
                seen.add(tok)
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": tok, "issue_key": "",
                                          "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        if not self._base or not self._email or not self._api_token:
            return False
        try:
            session = await self._get_session()
            async with session.get(
                    f"{self._base}/rest/api/3/myself") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
