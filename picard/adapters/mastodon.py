"""Mastodon adapter — stores chunks as direct-visibility statuses.

Each chunk becomes a Mastodon status posted with visibility="direct"
so it doesn't appear on public timelines.  The status text is the
PCHK-encoded chunk string.

Notice: This adapter must only be used with Mastodon accounts and
instances you own and control.  Use in a manner that violates the
Terms of Service of the instance you connect to is outside the scope
of this project and the responsibility of the user.

Requires: pip install picard[mastodon]
"""

from __future__ import annotations

import asyncio
import re
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return _HTML_TAG_RE.sub("", text).strip()


class MastodonAdapter(Adapter):
    """Scatter chunks into Mastodon statuses via Mastodon.py."""

    def __init__(
        self,
        api_base_url: str,
        access_token: str,
        *,
        max_chunk_bytes: int = 500,
    ) -> None:
        self._api_base_url = api_base_url
        self._access_token = access_token
        self._max = max_chunk_bytes
        self._client: object | None = None

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        from mastodon import Mastodon

        self._client = Mastodon(
            api_base_url=self._api_base_url,
            access_token=self._access_token,
        )
        return self._client

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="mastodon",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        client = self._ensure_client()

        def _post():
            return client.status_post(encoded_chunk, visibility="direct")

        status = await asyncio.to_thread(_post)
        return StoredChunkRef(adapter_name="mastodon", location=str(status["id"]))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        client = self._ensure_client()

        def _read():
            return client.status(int(ref.location))

        status = await asyncio.to_thread(_read)
        return _strip_html(status["content"])

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        client = self._ensure_client()

        def _delete():
            try:
                client.status_delete(int(ref.location))
                return True
            except Exception:
                return False

        return await asyncio.to_thread(_delete)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = self._ensure_client()
        prefix = f"PCHK:1:{manifest_id}:"

        def _list():
            me = client.me()
            refs: list[StoredChunkRef] = []
            page = client.account_statuses(me["id"])
            while page:
                for status in page:
                    text = _strip_html(status.get("content", ""))
                    if text.startswith(prefix):
                        refs.append(
                            StoredChunkRef(
                                adapter_name="mastodon",
                                location=str(status["id"]),
                            )
                        )
                page = client.fetch_next(page)
            return refs

        return await asyncio.to_thread(_list)

    async def health_check(self) -> bool:
        client = self._ensure_client()
        try:
            await asyncio.to_thread(client.instance)
            return True
        except Exception:
            return False
