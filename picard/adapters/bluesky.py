"""Bluesky adapter — stores chunks as AT Protocol posts.

Each chunk becomes a post on the authenticated user's Bluesky account.
The post text is the PCHK-encoded chunk string.  Posts are tagged with
a manifest-id prefix so they can be discovered for recovery.

Notice: This adapter must only be used with Bluesky accounts you own
and control.  Use in a manner that violates Bluesky's Terms of Service
is outside the scope of this project and the responsibility of the
user.  No endorsement by or affiliation with Bluesky PBLLC is implied.

Requires: pip install picard-store[bluesky]
"""

from __future__ import annotations

import re
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_AT_URI_RE = re.compile(r"at://[^/]+/app\.bsky\.feed\.post/([a-z0-9]+)")


class BlueskyAdapter(Adapter):
    """Scatter chunks into Bluesky posts via the AT Protocol."""

    def __init__(
        self,
        handle: str,
        app_password: str,
        *,
        max_chunk_bytes: int = 280,
        max_posts: int = 20,
    ) -> None:
        self._handle = handle
        self._app_password = app_password
        self._max = max_chunk_bytes
        self._client: object | None = None
        self._max_posts = max_posts
        self._post_count = 0

    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        from atproto import AsyncClient

        client = AsyncClient()
        await client.login(self._handle, self._app_password)
        self._client = client
        return client

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="bluesky",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        if self._post_count >= self._max_posts:
            raise RuntimeError(
                f"bluesky adapter reached max_posts={self._max_posts} per session"
            )
        client = await self._ensure_client()
        resp = await client.send_post(text=encoded_chunk)
        self._post_count += 1
        return StoredChunkRef(adapter_name="bluesky", location=resp.uri)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        client = await self._ensure_client()
        m = _AT_URI_RE.search(ref.location)
        if not m:
            raise ValueError(f"cannot parse AT URI: {ref.location}")
        rkey = m.group(1)
        resp = await client.get_post(rkey)
        return resp.value.text

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        client = await self._ensure_client()
        try:
            await client.delete_post(ref.location)
            return True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._ensure_client()
        prefix = f"PCHK:1:{manifest_id}:"
        refs: list[StoredChunkRef] = []
        cursor = None
        while True:
            resp = await client.get_author_feed(
                actor=self._handle,
                cursor=cursor,
                limit=50,
            )
            for item in resp.feed:
                text = getattr(item.post.record, "text", "")
                if text.startswith(prefix):
                    refs.append(
                        StoredChunkRef(
                            adapter_name="bluesky",
                            location=item.post.uri,
                        )
                    )
            cursor = resp.cursor
            if not cursor:
                break
        return refs

    async def health_check(self) -> bool:
        try:
            await self._ensure_client()
            return True
        except Exception:
            return False
