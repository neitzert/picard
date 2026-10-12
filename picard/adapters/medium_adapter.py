"""Medium adapter — stores chunks as draft stories.

Uses the Medium API to create draft posts.  Medium's API is
write-heavy (no read-back for drafts), so a local cache supplements
the write confirmation for retrieval.

Notice: This adapter must only be used with Medium accounts you own
and control.  Use in a manner that violates Medium's Terms of Service
is outside the scope of this project and the responsibility of the
user.  No endorsement by or affiliation with A Medium Corporation
is implied.

Requires: pip install picard[medium]   (uses aiohttp)
"""

from __future__ import annotations

from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_API_BASE = "https://api.medium.com/v1"


class MediumAdapter(Adapter):
    """Scatter chunks into Medium draft stories via API or Playwright."""

    def __init__(
        self,
        integration_token: str = "",
        author_id: str = "",
        *,
        username: str = "",
        password: str = "",
        use_browser: bool = False,
        headless: bool = True,
        max_chunk_bytes: int = 65536,
    ) -> None:
        self._token = integration_token
        self._author_id = author_id
        self._username = username
        self._password = password
        self._use_browser = use_browser
        self._headless = headless
        self._max = max_chunk_bytes
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
            name="medium",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=4 if self._use_browser else 10,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    # ── API mode ────────────────────────────────────────────────

    async def _api_write(self, encoded_chunk: str) -> StoredChunkRef:
        session = await self._get_session()
        async with session.post(
            f"{_API_BASE}/users/{self._author_id}/posts",
            headers=self._headers(),
            json={
                "title": "pchk",
                "contentFormat": "markdown",
                "content": encoded_chunk,
                "publishStatus": "draft",
            },
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            post_id = data.get("data", {}).get("id", "")
        loc = f"medium://{self._author_id}/{post_id}"
        self._cache[loc] = encoded_chunk
        return StoredChunkRef(adapter_name="medium", location=loc)

    async def _api_read(self, ref: StoredChunkRef) -> str:
        cached = self._cache.get(ref.location)
        if cached:
            return cached
        raise KeyError(f"medium draft not in cache: {ref.location}")

    async def _api_delete(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def _api_list(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        for loc, data in self._cache.items():
            if manifest_id in data:
                refs.append(StoredChunkRef(adapter_name="medium", location=loc))
        return refs

    # ── Browser mode ────────────────────────────────────────────

    async def _browser_page(self):
        from picard.browser_pool import BrowserPool

        pool = await BrowserPool.get(headless=self._headless)
        ctx = await pool.context("medium")
        page = await ctx.new_page()

        await page.goto("https://medium.com/", wait_until="domcontentloaded")
        logged_in = await page.locator('button[data-action="open-userActions"]').count() > 0

        if not logged_in:
            await page.goto("https://medium.com/m/signin", wait_until="domcontentloaded")
            email_btn = page.locator('button:has-text("Sign in with email")')
            if await email_btn.count() > 0:
                await email_btn.click()
                await page.fill('input[type="email"]', self._username)
                await page.click('button:has-text("Continue")')
                await page.wait_for_timeout(2000)
                pw_field = page.locator('input[type="password"]')
                if await pw_field.count() > 0:
                    await pw_field.fill(self._password)
                    await page.click('button:has-text("Continue")')
            await page.wait_for_timeout(5000)
            await pool.save_state("medium")

        return page, pool

    async def _browser_write(self, encoded_chunk: str) -> StoredChunkRef:
        import uuid

        page, pool = await self._browser_page()
        try:
            await page.goto("https://medium.com/new-story", wait_until="domcontentloaded")
            await page.wait_for_selector('[role="textbox"]', timeout=10000)
            editor = page.locator('[role="textbox"]').first
            await editor.fill(encoded_chunk)
            await page.wait_for_timeout(3000)
            await pool.save_state("medium")
            post_id = uuid.uuid4().hex[:12]
            loc = f"medium://{self._author_id}/{post_id}"
            self._cache[loc] = encoded_chunk
            return StoredChunkRef(adapter_name="medium", location=loc)
        finally:
            await page.close()

    async def _browser_read(self, ref: StoredChunkRef) -> str:
        cached = self._cache.get(ref.location)
        if cached:
            return cached
        page, _ = await self._browser_page()
        try:
            parts = ref.location.removeprefix("medium://").split("/")
            if len(parts) >= 2:
                post_id = parts[-1]
                await page.goto(f"https://medium.com/p/{post_id}/edit", wait_until="domcontentloaded")
                await page.wait_for_selector('[role="textbox"]', timeout=10000)
                content = await page.locator('[role="textbox"]').first.inner_text()
                return content
        finally:
            await page.close()
        raise KeyError(f"chunk not found: {ref.location}")

    async def _browser_delete(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def _browser_list(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        for loc, data in self._cache.items():
            if manifest_id in data:
                refs.append(StoredChunkRef(adapter_name="medium", location=loc))
        return refs

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
                f"{_API_BASE}/me", headers=self._headers()
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
