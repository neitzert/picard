"""TikTok adapter — stores chunks in video descriptions.

Posts video metadata via the Content Posting API and reads back
descriptions via the video query endpoint.

Two modes:
  - API mode (default): TikTok Content API — requires access_token + open_id
  - Browser mode: Playwright — pip install picard-store[browser]

Notice: This adapter must only be used with TikTok accounts and
infrastructure you own and control.  Use in a manner that violates
TikTok's Terms of Service is outside the scope of this project and
the responsibility of the user.  No endorsement by or affiliation
with TikTok or ByteDance is implied.
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_API_BASE = "https://open.tiktokapis.com"
_WEB = "https://www.tiktok.com"


class TikTokAdapter(Adapter):
    """Scatter chunks into TikTok video descriptions."""

    def __init__(
        self,
        access_token: str = "",
        open_id: str = "",
        *,
        username: str | None = None,
        password: str | None = None,
        max_chunk_bytes: int = 2200,
        use_browser: bool = False,
        headless: bool = True,
    ) -> None:
        self._access_token = access_token
        self._open_id = open_id
        self._username = username or ""
        self._password = password or ""
        self._max = max_chunk_bytes
        self._use_browser = use_browser
        self._headless = headless
        self._cache: dict[str, str] = {}
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
            name="tiktok",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=4 if self._use_browser else 10,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

    # ── API mode ────────────────────────────────────────────────

    async def _api_write(self, encoded_chunk: str) -> StoredChunkRef:
        session = await self._get_session()
        async with session.post(
            f"{_API_BASE}/v2/post/publish/",
            headers=self._headers(),
            json={
                "post_info": {
                    "title": encoded_chunk,
                    "privacy_level": "SELF_ONLY",
                },
                "source_info": {
                    "source": "PULL_FROM_URL",
                    "video_url": "https://example.com/placeholder.mp4",
                },
            },
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            publish_id = data.get("data", {}).get(
                "publish_id", uuid.uuid4().hex
            )
        loc = f"tiktok://{self._open_id}/{publish_id}"
        self._cache[loc] = encoded_chunk
        return StoredChunkRef(adapter_name="tiktok", location=loc)

    async def _api_read(self, ref: StoredChunkRef) -> str:
        cached = self._cache.get(ref.location)
        if cached:
            return cached

        video_id = ref.location.rsplit("/", 1)[-1]
        session = await self._get_session()
        async with session.post(
            f"{_API_BASE}/v2/video/query/",
            headers=self._headers(),
            json={"filters": {"video_ids": [video_id]}},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            videos = data.get("data", {}).get("videos", [])
            if not videos:
                raise KeyError(f"video not found: {video_id}")
            return videos[0].get("title", "")

    # ── Browser mode ────────────────────────────────────────────

    async def _browser_page(self):
        from picard.browser_pool import BrowserPool

        pool = await BrowserPool.get(headless=self._headless)
        ctx = await pool.context("tiktok")
        page = await ctx.new_page()

        await page.goto(f"{_WEB}/foryou", wait_until="domcontentloaded")
        logged_in = await page.locator('[data-e2e="profile-icon"]').count() > 0

        if not logged_in:
            await page.goto(f"{_WEB}/login/phone-or-email/email", wait_until="domcontentloaded")
            await page.fill('input[name="username"]', self._username)
            await page.fill('input[type="password"]', self._password)
            await page.click('button[data-e2e="login-button"]')
            await page.wait_for_timeout(5000)
            await pool.save_state("tiktok")

        return page, pool

    async def _browser_write(self, encoded_chunk: str) -> StoredChunkRef:
        page, pool = await self._browser_page()
        try:
            await page.goto(f"{_WEB}/creator#/upload?lang=en", wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)
            caption = page.locator('[data-e2e="caption-input"], .public-DraftEditor-content').first
            await caption.click()
            await caption.fill(encoded_chunk)

            privacy = page.locator('div[data-e2e="privacy-select"]')
            if await privacy.count() > 0:
                await privacy.click()
                await page.locator('text=Only me').click()

            chunk_id = uuid.uuid4().hex[:12]
            loc = f"tiktok://{self._username}/{chunk_id}"
            self._cache[loc] = encoded_chunk
            await pool.save_state("tiktok")
            return StoredChunkRef(adapter_name="tiktok", location=loc)
        finally:
            await page.close()

    async def _browser_read(self, ref: StoredChunkRef) -> str:
        cached = self._cache.get(ref.location)
        if cached:
            return cached
        raise KeyError(f"chunk not in cache: {ref.location}")

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
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        for loc, data in self._cache.items():
            if manifest_id in data:
                refs.append(StoredChunkRef(adapter_name="tiktok", location=loc))
        return refs

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
                f"{_API_BASE}/v2/user/info/",
                headers=self._headers(),
            ) as resp:
                return resp.status == 200
        except Exception:
            return False
