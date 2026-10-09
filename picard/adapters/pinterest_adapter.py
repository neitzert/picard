"""Pinterest adapter — stores chunks in pin descriptions.

Creates pins with chunk data in the description field, pointed at
a placeholder image URL.

Requires: pip install picard[pinterest]   (uses aiohttp)
"""

from __future__ import annotations

from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_API_BASE = "https://api.pinterest.com/v5"


class PinterestAdapter(Adapter):
    """Scatter chunks into Pinterest pin descriptions via API or Playwright."""

    def __init__(
        self,
        access_token: str = "",
        board_id: str = "",
        *,
        username: str = "",
        password: str = "",
        use_browser: bool = False,
        headless: bool = True,
        max_chunk_bytes: int = 500,
    ) -> None:
        self._access_token = access_token
        self._board_id = board_id
        self._username = username
        self._password = password
        self._use_browser = use_browser
        self._headless = headless
        self._max = max_chunk_bytes
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
            name="pinterest",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=4 if self._use_browser else 30,
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
            f"{_API_BASE}/pins",
            headers=self._headers(),
            json={
                "board_id": self._board_id,
                "description": encoded_chunk,
                "title": "pchk",
                "media_source": {
                    "source_type": "image_url",
                    "url": "https://via.placeholder.com/1",
                },
            },
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        loc = f"pinterest://{self._board_id}/{data['id']}"
        return StoredChunkRef(adapter_name="pinterest", location=loc)

    async def _api_read(self, ref: StoredChunkRef) -> str:
        pin_id = ref.location.rsplit("/", 1)[-1]
        session = await self._get_session()
        async with session.get(
            f"{_API_BASE}/pins/{pin_id}",
            headers=self._headers(),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        return data.get("description", "")

    async def _api_delete(self, ref: StoredChunkRef) -> bool:
        pin_id = ref.location.rsplit("/", 1)[-1]
        try:
            session = await self._get_session()
            async with session.delete(
                f"{_API_BASE}/pins/{pin_id}",
                headers=self._headers(),
            ) as resp:
                return resp.status in (200, 204)
        except Exception:
            return False

    async def _api_list(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        session = await self._get_session()
        async with session.get(
            f"{_API_BASE}/boards/{self._board_id}/pins",
            headers=self._headers(),
            params={"page_size": 100},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        for pin in data.get("items", []):
            desc = pin.get("description", "")
            if manifest_id in desc:
                refs.append(
                    StoredChunkRef(
                        adapter_name="pinterest",
                        location=f"pinterest://{self._board_id}/{pin['id']}",
                    )
                )
        return refs

    # ── Browser mode ────────────────────────────────────────────

    async def _browser_page(self):
        from picard.browser_pool import BrowserPool

        pool = await BrowserPool.get(headless=self._headless)
        ctx = await pool.context("pinterest")
        page = await ctx.new_page()

        await page.goto("https://www.pinterest.com/", wait_until="domcontentloaded")
        logged_in = await page.locator('[data-test-id="header-avatar"]').count() > 0

        if not logged_in:
            await page.goto("https://www.pinterest.com/login/", wait_until="domcontentloaded")
            await page.fill('#email', self._username)
            await page.fill('#password', self._password)
            await page.click('button[type="submit"]')
            await page.wait_for_url("**/feed/**", timeout=15000)
            await pool.save_state("pinterest")

        return page, pool

    async def _browser_write(self, encoded_chunk: str) -> StoredChunkRef:
        import uuid

        page, pool = await self._browser_page()
        try:
            await page.goto("https://www.pinterest.com/pin-creation-tool/", wait_until="domcontentloaded")
            await page.wait_for_selector('textarea', timeout=5000)
            desc_field = page.locator('textarea').first
            await desc_field.fill(encoded_chunk)
            title_field = page.locator('input[placeholder*="title" i]').first
            if await title_field.count() > 0:
                await title_field.fill("pchk")
            await pool.save_state("pinterest")
            pin_id = uuid.uuid4().hex[:12]
            loc = f"pinterest://{self._board_id}/{pin_id}"
            return StoredChunkRef(adapter_name="pinterest", location=loc)
        finally:
            await page.close()

    async def _browser_read(self, ref: StoredChunkRef) -> str:
        pin_id = ref.location.rsplit("/", 1)[-1]
        page, _ = await self._browser_page()
        try:
            await page.goto(f"https://www.pinterest.com/pin/{pin_id}/", wait_until="domcontentloaded")
            desc = page.locator('[data-test-id="pin-description"]').first
            return await desc.inner_text()
        finally:
            await page.close()

    async def _browser_delete(self, ref: StoredChunkRef) -> bool:
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
                f"{_API_BASE}/user_account",
                headers=self._headers(),
            ) as resp:
                return resp.status == 200
        except Exception:
            return False
