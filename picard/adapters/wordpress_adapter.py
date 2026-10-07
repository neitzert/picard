"""WordPress adapter — stores chunks as draft posts via WP REST API.

Posts are created with status="draft" so they're invisible to the public.
Any self-hosted WordPress site with the REST API enabled works.

Requires: pip install picard[wordpress]   (uses aiohttp)
"""

from __future__ import annotations

import base64
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class WordPressAdapter(Adapter):
    """Scatter chunks into WordPress draft posts via API or Playwright."""

    def __init__(
        self,
        site_url: str = "",
        username: str = "",
        application_password: str = "",
        *,
        password: str = "",
        use_drafts: bool = True,
        use_browser: bool = False,
        headless: bool = True,
        max_chunk_bytes: int = 65536,
    ) -> None:
        self._site_url = site_url.rstrip("/") if site_url else ""
        self._username = username
        self._app_password = application_password
        self._password = password
        self._use_drafts = use_drafts
        self._use_browser = use_browser
        self._headless = headless
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="wordpress",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=4 if self._use_browser else 60,
        )

    def _headers(self) -> dict[str, str]:
        cred = base64.b64encode(
            f"{self._username}:{self._app_password}".encode()
        ).decode()
        return {
            "Authorization": f"Basic {cred}",
            "Content-Type": "application/json",
        }

    def _api_url(self, path: str = "") -> str:
        return f"{self._site_url}/wp-json/wp/v2{path}"

    # ── API mode ────────────────────────────────────────────────

    async def _api_write(self, encoded_chunk: str) -> StoredChunkRef:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.post(
                self._api_url("/posts"),
                headers=self._headers(),
                json={
                    "title": "pchk",
                    "content": encoded_chunk,
                    "status": "draft" if self._use_drafts else "private",
                },
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        loc = f"wp://{self._site_url}/{data['id']}"
        return StoredChunkRef(adapter_name="wordpress", location=loc)

    async def _api_read(self, ref: StoredChunkRef) -> str:
        import aiohttp

        post_id = ref.location.rsplit("/", 1)[-1]
        async with aiohttp.ClientSession() as session:
            async with session.get(
                self._api_url(f"/posts/{post_id}"),
                headers=self._headers(),
                params={"context": "edit"},
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        raw = data.get("content", {}).get("raw", "")
        return raw if raw else data.get("content", {}).get("rendered", "")

    async def _api_delete(self, ref: StoredChunkRef) -> bool:
        import aiohttp

        post_id = ref.location.rsplit("/", 1)[-1]
        try:
            async with aiohttp.ClientSession() as session:
                async with session.delete(
                    self._api_url(f"/posts/{post_id}"),
                    headers=self._headers(),
                    params={"force": "true"},
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def _api_list(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        import aiohttp

        refs: list[StoredChunkRef] = []
        page = 1
        async with aiohttp.ClientSession() as session:
            while True:
                async with session.get(
                    self._api_url("/posts"),
                    headers=self._headers(),
                    params={
                        "status": "draft,private",
                        "per_page": 100,
                        "page": page,
                        "context": "edit",
                    },
                ) as resp:
                    if resp.status != 200:
                        break
                    posts = await resp.json()
                if not posts:
                    break
                for post in posts:
                    raw = post.get("content", {}).get("raw", "")
                    if manifest_id in raw:
                        refs.append(
                            StoredChunkRef(
                                adapter_name="wordpress",
                                location=f"wp://{self._site_url}/{post['id']}",
                            )
                        )
                if len(posts) < 100:
                    break
                page += 1
        return refs

    # ── Browser mode ────────────────────────────────────────────

    async def _browser_page(self):
        from picard.browser_pool import BrowserPool

        pool = await BrowserPool.get(headless=self._headless)
        ctx = await pool.context("wordpress")
        page = await ctx.new_page()

        login_url = f"{self._site_url}/wp-login.php"
        await page.goto(f"{self._site_url}/wp-admin/", wait_until="domcontentloaded")
        logged_in = "wp-admin" in page.url and "wp-login" not in page.url

        if not logged_in:
            await page.goto(login_url, wait_until="domcontentloaded")
            await page.fill('#user_login', self._username)
            await page.fill('#user_pass', self._password or self._app_password)
            await page.click('#wp-submit')
            await page.wait_for_url("**/wp-admin/**", timeout=15000)
            await pool.save_state("wordpress")

        return page, pool

    async def _browser_write(self, encoded_chunk: str) -> StoredChunkRef:
        import uuid

        page, pool = await self._browser_page()
        try:
            await page.goto(f"{self._site_url}/wp-admin/post-new.php", wait_until="domcontentloaded")
            title_field = page.locator('#title')
            if await title_field.count() > 0:
                await title_field.fill("pchk")
            content_field = page.locator('#content')
            if await content_field.count() > 0:
                await content_field.fill(encoded_chunk)
            save_btn = page.locator('#save-post')
            if await save_btn.count() > 0:
                await save_btn.click()
                await page.wait_for_timeout(3000)
            await pool.save_state("wordpress")
            post_id = uuid.uuid4().hex[:8]
            loc = f"wp://{self._site_url}/{post_id}"
            return StoredChunkRef(adapter_name="wordpress", location=loc)
        finally:
            await page.close()

    async def _browser_read(self, ref: StoredChunkRef) -> str:
        post_id = ref.location.rsplit("/", 1)[-1]
        page, _ = await self._browser_page()
        try:
            await page.goto(f"{self._site_url}/wp-admin/post.php?post={post_id}&action=edit", wait_until="domcontentloaded")
            content = page.locator('#content')
            if await content.count() > 0:
                return await content.input_value()
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
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self._api_url("/users/me"),
                    headers=self._headers(),
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False
