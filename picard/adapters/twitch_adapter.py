"""Twitch adapter — stores chunks in channel descriptions.

Updates the channel description field via the Helix API.  Each chunk
overwrites the description, so only one chunk per broadcaster.
Use chat messages for multi-chunk storage (ephemeral).

Notice: This adapter must only be used with Twitch channels you own
and control.  Use in a manner that violates Twitch's Terms of Service
is outside the scope of this project and the responsibility of the
user.  No endorsement by or affiliation with Twitch Interactive, Inc.
or Amazon is implied.

Requires: pip install picard[twitch]   (uses aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_API_BASE = "https://api.twitch.tv/helix"


class TwitchAdapter(Adapter):
    """Scatter chunks into Twitch channel descriptions or chat, via API or Playwright."""

    def __init__(
        self,
        client_id: str = "",
        access_token: str = "",
        broadcaster_id: str = "",
        *,
        username: str = "",
        password: str = "",
        use_chat: bool = False,
        use_browser: bool = False,
        headless: bool = True,
        max_chunk_bytes: int = 300,
    ) -> None:
        self._client_id = client_id
        self._access_token = access_token
        self._broadcaster_id = broadcaster_id
        self._username = username
        self._password = password
        self._use_chat = use_chat
        self._use_browser = use_browser
        self._headless = headless
        self._max = max_chunk_bytes
        self._chat_cache: dict[str, str] = {}
        self._session = None


    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _get_session(self):
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="twitch",
            max_chunk_bytes=self._max,
            persistent=not self._use_chat,
            ordered=False,
            rate_limit_per_min=4 if self._use_browser else 30,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Client-Id": self._client_id,
            "Content-Type": "application/json",
        }

    # ── API mode ────────────────────────────────────────────────

    async def _api_write(self, encoded_chunk: str) -> StoredChunkRef:
        if self._use_chat:
            loc = f"twitch://{self._broadcaster_id}/chat/{uuid.uuid4().hex[:8]}"
            session = await self._get_session()
            async with session.post(
                f"{_API_BASE}/chat/messages",
                headers=self._headers(),
                json={
                    "broadcaster_id": self._broadcaster_id,
                    "sender_id": self._broadcaster_id,
                    "message": encoded_chunk,
                },
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                msg_id = data.get("data", [{}])[0].get("message_id", uuid.uuid4().hex)
                loc = f"twitch://{self._broadcaster_id}/chat/{msg_id}"
            self._chat_cache[loc] = encoded_chunk
            return StoredChunkRef(adapter_name="twitch", location=loc)

        session = await self._get_session()
        async with session.patch(
            f"{_API_BASE}/channels",
            headers=self._headers(),
            params={"broadcaster_id": self._broadcaster_id},
            json={"description": encoded_chunk},
        ) as resp:
            resp.raise_for_status()
        loc = f"twitch://{self._broadcaster_id}/description"
        return StoredChunkRef(adapter_name="twitch", location=loc)

    async def _api_read(self, ref: StoredChunkRef) -> str:
        if "/chat/" in ref.location:
            cached = self._chat_cache.get(ref.location)
            if cached:
                return cached
            raise KeyError(f"chat message not in cache: {ref.location}")

        session = await self._get_session()
        async with session.get(
            f"{_API_BASE}/channels",
            headers=self._headers(),
            params={"broadcaster_id": self._broadcaster_id},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        return data["data"][0]["description"]

    async def _api_delete(self, ref: StoredChunkRef) -> bool:
        if "/chat/" in ref.location:
            return self._chat_cache.pop(ref.location, None) is not None

        try:
            session = await self._get_session()
            async with session.patch(
                f"{_API_BASE}/channels",
                headers=self._headers(),
                params={"broadcaster_id": self._broadcaster_id},
                json={"description": ""},
            ) as resp:
                return resp.status == 204
        except Exception:
            return False

    async def _api_list(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        for loc, data in self._chat_cache.items():
            if manifest_id in data:
                refs.append(StoredChunkRef(adapter_name="twitch", location=loc))
        return refs

    # ── Browser mode ────────────────────────────────────────────

    async def _browser_page(self):
        from picard.browser_pool import BrowserPool

        pool = await BrowserPool.get(headless=self._headless)
        ctx = await pool.context("twitch")
        page = await ctx.new_page()

        await page.goto("https://www.twitch.tv/", wait_until="domcontentloaded")
        logged_in = await page.locator('[data-a-target="user-menu-toggle"]').count() > 0

        if not logged_in:
            await page.goto("https://www.twitch.tv/login", wait_until="domcontentloaded")
            await page.fill('#login-username', self._username)
            await page.fill('#password-input', self._password)
            await page.click('[data-a-target="passport-login-button"]')
            await page.wait_for_timeout(5000)
            await pool.save_state("twitch")

        return page, pool

    async def _browser_write(self, encoded_chunk: str) -> StoredChunkRef:
        import uuid

        page, pool = await self._browser_page()
        try:
            await page.goto(f"https://dashboard.twitch.tv/u/{self._username}/settings/channel", wait_until="domcontentloaded")
            desc_field = page.locator('textarea[name="description"]').first
            if await desc_field.count() > 0:
                await desc_field.fill(encoded_chunk)
                save_btn = page.locator('button:has-text("Save")')
                if await save_btn.count() > 0:
                    await save_btn.click()
                    await page.wait_for_timeout(2000)
            await pool.save_state("twitch")
            loc = f"twitch://{self._broadcaster_id}/description"
            return StoredChunkRef(adapter_name="twitch", location=loc)
        finally:
            await page.close()

    async def _browser_read(self, ref: StoredChunkRef) -> str:
        if "/chat/" in ref.location:
            cached = self._chat_cache.get(ref.location)
            if cached:
                return cached
            raise KeyError(f"chat message not in cache: {ref.location}")
        page, _ = await self._browser_page()
        try:
            await page.goto(f"https://www.twitch.tv/{self._username}/about", wait_until="domcontentloaded")
            desc = page.locator('[data-a-target="profile-description"]').first
            if await desc.count() > 0:
                return await desc.inner_text()
            raise KeyError(f"chunk not found: {ref.location}")
        finally:
            await page.close()

    async def _browser_delete(self, ref: StoredChunkRef) -> bool:
        if "/chat/" in ref.location:
            return self._chat_cache.pop(ref.location, None) is not None
        return False

    async def _browser_list(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    # ── Dispatch ────────────────────────────────────────────────

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        if self._use_browser:
            return await self._browser_write(encoded_chunk)
        return await self._api_write(encoded_chunk)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if self._use_browser:
            return await self._browser_read(ref)
        return await self._api_read(ref)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        if self._use_browser:
            return await self._browser_delete(ref)
        return await self._api_delete(ref)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        if self._use_browser:
            return await self._browser_list(manifest_id)
        return await self._api_list(manifest_id)

    async def health_check(self) -> bool:
        if self._use_browser:
            try:
                page, _ = await self._browser_page()
                await page.close()
                return True
            except Exception:
                return False
        try:
            session = await self._get_session()
            async with session.get(
                f"{_API_BASE}/users",
                headers=self._headers(),
            ) as resp:
                return resp.status == 200
        except Exception:
            return False
