"""Reddit adapter — stores chunks as self-posts on a subreddit.

Each chunk becomes a self-post whose body is the PCHK-encoded string.
Posts are titled with a manifest-id tag so they can be filtered for
recovery.

Requires: pip install picard[reddit]
"""

from __future__ import annotations

import asyncio
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class RedditAdapter(Adapter):
    """Scatter chunks into Reddit self-posts via PRAW."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        username: str,
        password: str,
        subreddit: str,
        *,
        user_agent: str = "picard/0.1",
        max_chunk_bytes: int = 39_000,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._username = username
        self._password = password
        self._subreddit_name = subreddit
        self._user_agent = user_agent
        self._max = max_chunk_bytes
        self._reddit: object | None = None

    def _ensure_reddit(self):
        if self._reddit is not None:
            return self._reddit
        import praw

        self._reddit = praw.Reddit(
            client_id=self._client_id,
            client_secret=self._client_secret,
            user_agent=self._user_agent,
            username=self._username,
            password=self._password,
        )
        return self._reddit

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="reddit",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=10,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        reddit = self._ensure_reddit()
        chunk = Chunk.decode(encoded_chunk)
        title = (
            f"[PCHK] {chunk.manifest_id} "
            f"b{chunk.block_index} c{chunk.chunk_index}"
        )

        def _submit():
            sub = reddit.subreddit(self._subreddit_name)
            submission = sub.submit(title=title, selftext=encoded_chunk)
            return submission.id

        submission_id = await asyncio.to_thread(_submit)
        return StoredChunkRef(adapter_name="reddit", location=submission_id)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        reddit = self._ensure_reddit()

        def _read():
            return reddit.submission(id=ref.location).selftext

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        reddit = self._ensure_reddit()

        def _delete():
            try:
                reddit.submission(id=ref.location).delete()
                return True
            except Exception:
                return False

        return await asyncio.to_thread(_delete)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        reddit = self._ensure_reddit()
        tag = f"[PCHK] {manifest_id} "

        def _list():
            refs: list[StoredChunkRef] = []
            for submission in reddit.redditor(self._username).submissions.new(
                limit=1000
            ):
                if submission.title.startswith(tag):
                    refs.append(
                        StoredChunkRef(
                            adapter_name="reddit",
                            location=submission.id,
                        )
                    )
            return refs

        return await asyncio.to_thread(_list)

    async def health_check(self) -> bool:
        try:
            reddit = self._ensure_reddit()
            await asyncio.to_thread(lambda: reddit.user.me())
            return True
        except Exception:
            return False
