"""LinkedIn adapter — stores chunks as LinkedIn posts.

Creates unlisted/author-only UGC posts with chunk data in the
share commentary text field.

Requires: pip install picard[linkedin]   (uses aiohttp)
"""

from __future__ import annotations

from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_API_BASE = "https://api.linkedin.com/v2"


class LinkedInAdapter(Adapter):
    """Scatter chunks into LinkedIn post text."""

    def __init__(
        self,
        access_token: str,
        person_urn: str,
        *,
        max_chunk_bytes: int = 3000,
    ) -> None:
        self._access_token = access_token
        self._person_urn = person_urn
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="linkedin",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=25,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
            "X-Restli-Protocol-Version": "2.0.0",
        }

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_API_BASE}/ugcPosts",
                headers=self._headers(),
                json={
                    "author": self._person_urn,
                    "lifecycleState": "PUBLISHED",
                    "visibility": {
                        "com.linkedin.ugc.MemberNetworkVisibility": "CONNECTIONS"
                    },
                    "specificContent": {
                        "com.linkedin.ugc.ShareContent": {
                            "shareCommentary": {"text": encoded_chunk},
                            "shareMediaCategory": "NONE",
                        }
                    },
                },
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                post_id = data.get("id", "")
        loc = f"linkedin://{self._person_urn}/{post_id}"
        return StoredChunkRef(adapter_name="linkedin", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import aiohttp

        post_id = ref.location.rsplit("/", 1)[-1]
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{_API_BASE}/ugcPosts/{post_id}",
                headers=self._headers(),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        return (
            data.get("specificContent", {})
            .get("com.linkedin.ugc.ShareContent", {})
            .get("shareCommentary", {})
            .get("text", "")
        )

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        import aiohttp

        post_id = ref.location.rsplit("/", 1)[-1]
        try:
            async with aiohttp.ClientSession() as session:
                async with session.delete(
                    f"{_API_BASE}/ugcPosts/{post_id}",
                    headers=self._headers(),
                ) as resp:
                    return resp.status in (200, 204)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        import aiohttp

        refs: list[StoredChunkRef] = []
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{_API_BASE}/ugcPosts",
                headers=self._headers(),
                params={"q": "authors", "authors": self._person_urn},
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        for item in data.get("elements", []):
            text = (
                item.get("specificContent", {})
                .get("com.linkedin.ugc.ShareContent", {})
                .get("shareCommentary", {})
                .get("text", "")
            )
            if manifest_id in text:
                refs.append(
                    StoredChunkRef(
                        adapter_name="linkedin",
                        location=f"linkedin://{self._person_urn}/{item['id']}",
                    )
                )
        return refs

    async def health_check(self) -> bool:
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{_API_BASE}/me", headers=self._headers()
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False
