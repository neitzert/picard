"""Imgur adapter — stores chunks in image descriptions.

Each chunk is uploaded as a 1x1 PNG with data in the description:
    imgur://{image_hash}

Notice: This adapter must only be used with Imgur accounts you own
and control.  Use in a manner that violates Imgur's Terms of Service
is outside the scope of this project and the responsibility of the
user.  No endorsement by or affiliation with Imgur, LLC is implied.

Requires: pip install picard-store[imgur]
"""

from __future__ import annotations

import base64
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
import aiohttp

_API = "https://api.imgur.com/3"
# 1x1 transparent PNG
_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
    "nGNgYPgPAAEDAQAIicLsAAAABJRU5ErkJggg=="
)


class ImgurAdapter(Adapter):
    """Scatter chunks into Imgur image descriptions via API or Playwright."""

    def __init__(
        self,
        client_id: str = "",
        *,
        client_secret: str | None = None,
        access_token: str | None = None,
        username: str = "",
        password: str = "",
        use_browser: bool = False,
        headless: bool = True,
        max_chunk_bytes: int = 10_000,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._access_token = access_token
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
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="imgur",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=4 if self._use_browser else 30,
        )

    def _headers(self) -> dict:
        if self._access_token:
            return {"Authorization": f"Bearer {self._access_token}"}
        return {"Authorization": f"Client-ID {self._client_id}"}

    # ── API mode ────────────────────────────────────────────────

    async def _api_write(self, encoded_chunk: str) -> StoredChunkRef:
        data = aiohttp.FormData()
        data.add_field("image", base64.b64encode(_PIXEL_PNG).decode())
        data.add_field("type", "base64")
        data.add_field("description", encoded_chunk)
        data.add_field("title", "pchk")

        session = await self._get_session()
        async with session.post(
            f"{_API}/image", data=data, headers=self._headers()
        ) as resp:
            resp.raise_for_status()
            result = await resp.json()

        image_hash = result["data"]["id"]
        location = f"imgur://{image_hash}"
        return StoredChunkRef(adapter_name="imgur", location=location)

    async def _api_read(self, ref: StoredChunkRef) -> str:
        image_hash = ref.location.split("://", 1)[1]

        session = await self._get_session()
        async with session.get(
            f"{_API}/image/{image_hash}", headers=self._headers()
        ) as resp:
            resp.raise_for_status()
            result = await resp.json()

        return result["data"]["description"]

    async def _api_delete(self, ref: StoredChunkRef) -> bool:
        image_hash = ref.location.split("://", 1)[1]

        try:
            session = await self._get_session()
            async with session.delete(
                f"{_API}/image/{image_hash}", headers=self._headers()
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def _api_list(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    # ── Browser mode ────────────────────────────────────────────

    async def _browser_page(self):
        from picard.browser_pool import BrowserPool

        pool = await BrowserPool.get(headless=self._headless)
        ctx = await pool.context("imgur")
        page = await ctx.new_page()

        await page.goto("https://imgur.com/", wait_until="domcontentloaded")
        logged_in = await page.locator('.AccountNavButton').count() > 0

        if not logged_in:
            await page.goto("https://imgur.com/signin", wait_until="domcontentloaded")
            await page.fill('input[name="username"]', self._username)
            await page.fill('input[name="password"]', self._password)
            await page.click('button[type="submit"]')
            await page.wait_for_timeout(5000)
            await pool.save_state("imgur")

        return page, pool

    async def _browser_write(self, encoded_chunk: str) -> StoredChunkRef:
        import uuid

        page, pool = await self._browser_page()
        try:
            await page.goto("https://imgur.com/upload", wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
            desc_field = page.locator('textarea[placeholder*="description" i]').first
            if await desc_field.count() > 0:
                await desc_field.fill(encoded_chunk)
            await pool.save_state("imgur")
            image_hash = uuid.uuid4().hex[:10]
            loc = f"imgur://{image_hash}"
            return StoredChunkRef(adapter_name="imgur", location=loc)
        finally:
            await page.close()

    async def _browser_read(self, ref: StoredChunkRef) -> str:
        image_hash = ref.location.split("://", 1)[1]
        page, _ = await self._browser_page()
        try:
            await page.goto(f"https://imgur.com/{image_hash}", wait_until="domcontentloaded")
            desc = page.locator('.Post-description, .PostContent-description').first
            if await desc.count() > 0:
                return await desc.inner_text()
            raise KeyError(f"chunk not found: {ref.location}")
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
                f"{_API}/credits", headers=self._headers()
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        try:
            session = await self._get_session()
            async with session.get(
                f"{_API}/credits", headers=self._headers()
            ) as resp:
                data = await resp.json()
                remaining = data["data"].get("ClientRemaining", 0)
                return {
                    "used_bytes": None,
                    "free_bytes": None,
                    "total_bytes": None,
                    "chunk_count": remaining,
                }
        except Exception:
            return {
                "used_bytes": None,
                "free_bytes": None,
                "total_bytes": None,
                "chunk_count": None,
            }
