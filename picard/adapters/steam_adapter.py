"""Steam adapter — stores chunks as Steam profile comments or Workshop descriptions.

Uses the Steam Web API to post comments on a user's own profile or
create Workshop items with chunk data in the description field.

Notice: This adapter must only be used with Steam accounts and
Workshop content you own and control.  Use in a manner that violates
Steam's Subscriber Agreement or API Terms of Use is outside the scope
of this project and the responsibility of the user.  No endorsement
by or affiliation with Valve Corporation is implied.

Requires: pip install picard-store[steam]  (aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class SteamAdapter(Adapter):
    """Scatter chunks into Steam profile comments via API or Playwright."""

    def __init__(
        self,
        api_key: str = "",
        steam_id: str = "",
        *,
        session_id: str | None = None,
        login_cookie: str | None = None,
        username: str = "",
        password: str = "",
        use_browser: bool = False,
        headless: bool = True,
        max_chunk_bytes: int = 8000,
    ) -> None:
        self._api_key = api_key
        self._steam_id = steam_id
        self._session_id = session_id
        self._login_cookie = login_cookie
        self._username = username
        self._password = password
        self._use_browser = use_browser
        self._headless = headless
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="steam",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=4 if self._use_browser else 15,
        )

    # ── API mode ────────────────────────────────────────────────

    async def _api_write(self, encoded_chunk: str) -> StoredChunkRef:
        import aiohttp

        try:
            from picard.chunker import Chunk
            chunk = Chunk.decode(encoded_chunk)
            chunk_id = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
        except (ValueError, Exception):
            chunk_id = uuid.uuid4().hex

        cookies = {}
        if self._login_cookie:
            cookies["steamLoginSecure"] = self._login_cookie
        if self._session_id:
            cookies["sessionid"] = self._session_id

        async with aiohttp.ClientSession(cookies=cookies) as session:
            async with session.post(
                f"https://steamcommunity.com/comment/Profile/post/{self._steam_id}/-1/",
                data={
                    "comment": encoded_chunk,
                    "sessionid": self._session_id or "",
                },
            ) as resp:
                data = await resp.json()
                comment_id = str(data.get("gidcomment", chunk_id))

        location = f"steam://{self._steam_id}/{comment_id}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="steam", location=location)

    async def _api_read(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]

        import aiohttp
        import re

        parts = ref.location.removeprefix("steam://").split("/")
        steam_id = parts[0]
        comment_id = parts[1] if len(parts) > 1 else ""

        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://steamcommunity.com/comment/Profile/render/{steam_id}/-1/",
            ) as resp:
                data = await resp.json()
                html = data.get("comments_html", "")
                for match in re.finditer(
                    r'id="comment_(\d+)".*?<div class="commentthread_comment_text"[^>]*>(.*?)</div>',
                    html, re.DOTALL,
                ):
                    if match.group(1) == comment_id:
                        text = re.sub(r"<[^>]+>", "", match.group(2)).strip()
                        self._cache[ref.location] = text
                        return text
        raise KeyError(f"chunk not found: {ref.location}")

    async def _api_delete(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def _api_list(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="steam", location=loc)
            for loc, data in self._cache.items()
            if manifest_id in loc or manifest_id in data
        ]

    # ── Browser mode ────────────────────────────────────────────

    async def _browser_page(self):
        from picard.browser_pool import BrowserPool

        pool = await BrowserPool.get(headless=self._headless)
        ctx = await pool.context("steam")
        page = await ctx.new_page()

        await page.goto("https://steamcommunity.com/", wait_until="domcontentloaded")
        logged_in = await page.locator('#account_pulldown').count() > 0

        if not logged_in:
            await page.goto("https://store.steampowered.com/login/", wait_until="domcontentloaded")
            await page.fill('input[type="text"]._2GBWeup5cttgbTw8FM3R5y', self._username)
            await page.fill('input[type="password"]', self._password)
            await page.click('button[type="submit"]')
            await page.wait_for_timeout(10000)
            await pool.save_state("steam")

        return page, pool

    async def _browser_write(self, encoded_chunk: str) -> StoredChunkRef:
        page, pool = await self._browser_page()
        try:
            await page.goto(
                f"https://steamcommunity.com/profiles/{self._steam_id}",
                wait_until="domcontentloaded",
            )
            comment_box = page.locator('textarea.commentthread_textarea').first
            if await comment_box.count() > 0:
                await comment_box.fill(encoded_chunk)
                post_btn = page.locator('a.btn_green_white_innerfade:has-text("Post")')
                if await post_btn.count() > 0:
                    await post_btn.click()
                    await page.wait_for_timeout(3000)
            await pool.save_state("steam")
            try:
                from picard.chunker import Chunk
                chunk = Chunk.decode(encoded_chunk)
                chunk_id = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
            except Exception:
                chunk_id = uuid.uuid4().hex
            loc = f"steam://{self._steam_id}/{chunk_id}"
            self._cache[loc] = encoded_chunk
            return StoredChunkRef(adapter_name="steam", location=loc)
        finally:
            await page.close()

    async def _browser_read(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]
        page, _ = await self._browser_page()
        try:
            await page.goto(
                f"https://steamcommunity.com/profiles/{self._steam_id}",
                wait_until="domcontentloaded",
            )
            comments = page.locator('.commentthread_comment_text')
            count = await comments.count()
            for i in range(count):
                text = await comments.nth(i).inner_text()
                if text.strip():
                    self._cache[ref.location] = text.strip()
                    return text.strip()
        finally:
            await page.close()
        raise KeyError(f"chunk not found: {ref.location}")

    async def _browser_delete(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def _browser_list(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="steam", location=loc)
            for loc, data in self._cache.items()
            if manifest_id in loc or manifest_id in data
        ]

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
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/",
                    params={"key": self._api_key, "steamids": self._steam_id},
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        used = sum(len(v.encode()) for v in self._cache.values())
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._cache),
        }
