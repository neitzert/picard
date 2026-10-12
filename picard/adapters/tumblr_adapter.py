"""Tumblr adapter — stores chunks as draft text posts.

Posts are created with state="draft" so they don't appear publicly.
The blog owner can see and manage them from the dashboard.

Notice: This adapter must only be used with Tumblr blogs you own and
control.  Use in a manner that violates Tumblr's Terms of Service is
outside the scope of this project and the responsibility of the user.
No endorsement by or affiliation with Automattic is implied.

Requires: pip install picard-store[tumblr]   (uses aiohttp)
"""

from __future__ import annotations

from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_API_BASE = "https://api.tumblr.com/v2"


class TumblrAdapter(Adapter):
    """Scatter chunks into Tumblr draft posts via API or Playwright."""

    def __init__(
        self,
        consumer_key: str = "",
        consumer_secret: str = "",
        oauth_token: str = "",
        oauth_secret: str = "",
        blog_name: str = "",
        *,
        username: str = "",
        password: str = "",
        use_browser: bool = False,
        headless: bool = True,
        max_chunk_bytes: int = 65536,
    ) -> None:
        self._consumer_key = consumer_key
        self._consumer_secret = consumer_secret
        self._oauth_token = oauth_token
        self._oauth_secret = oauth_secret
        self._blog = blog_name
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
            name="tumblr",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=4 if self._use_browser else 30,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._oauth_token}",
            "Content-Type": "application/json",
        }

    # ── API mode ────────────────────────────────────────────────

    async def _api_write(self, encoded_chunk: str) -> StoredChunkRef:
        session = await self._get_session()
        async with session.post(
            f"{_API_BASE}/blog/{self._blog}/post",
            headers=self._headers(),
            json={
                "type": "text",
                "title": "pchk",
                "body": encoded_chunk,
                "state": "draft",
            },
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            post_id = data.get("response", {}).get("id", "")
        loc = f"tumblr://{self._blog}/{post_id}"
        return StoredChunkRef(adapter_name="tumblr", location=loc)

    async def _api_read(self, ref: StoredChunkRef) -> str:
        post_id = ref.location.rsplit("/", 1)[-1]
        session = await self._get_session()
        async with session.get(
            f"{_API_BASE}/blog/{self._blog}/posts/{post_id}",
            headers=self._headers(),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        posts = data.get("response", {}).get("posts", [])
        if not posts:
            raise KeyError(f"post not found: {post_id}")
        return posts[0].get("body", "")

    async def _api_delete(self, ref: StoredChunkRef) -> bool:
        post_id = ref.location.rsplit("/", 1)[-1]
        try:
            session = await self._get_session()
            async with session.post(
                f"{_API_BASE}/blog/{self._blog}/post/delete",
                headers=self._headers(),
                json={"id": post_id},
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def _api_list(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        session = await self._get_session()
        async with session.get(
            f"{_API_BASE}/blog/{self._blog}/drafts",
            headers=self._headers(),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        for post in data.get("response", {}).get("posts", []):
            body = post.get("body", "")
            if manifest_id in body:
                refs.append(
                    StoredChunkRef(
                        adapter_name="tumblr",
                        location=f"tumblr://{self._blog}/{post['id']}",
                    )
                )
        return refs

    # ── Browser mode ────────────────────────────────────────────

    async def _browser_page(self):
        from picard.browser_pool import BrowserPool

        pool = await BrowserPool.get(headless=self._headless)
        ctx = await pool.context("tumblr")
        page = await ctx.new_page()

        await page.goto("https://www.tumblr.com/dashboard", wait_until="domcontentloaded")
        logged_in = "dashboard" in page.url

        if not logged_in:
            await page.goto("https://www.tumblr.com/login", wait_until="domcontentloaded")
            await page.fill('input[name="email"]', self._username)
            await page.click('button:has-text("Next")')
            await page.wait_for_timeout(2000)
            await page.fill('input[name="password"]', self._password)
            await page.click('button:has-text("Log in")')
            await page.wait_for_url("**/dashboard**", timeout=15000)
            await pool.save_state("tumblr")

        return page, pool

    async def _browser_write(self, encoded_chunk: str) -> StoredChunkRef:
        import uuid

        page, pool = await self._browser_page()
        try:
            await page.goto("https://www.tumblr.com/new/text", wait_until="domcontentloaded")
            await page.wait_for_selector('[contenteditable="true"]', timeout=5000)
            editor = page.locator('[contenteditable="true"]').first
            await editor.fill(encoded_chunk)
            await pool.save_state("tumblr")
            post_id = uuid.uuid4().hex[:12]
            loc = f"tumblr://{self._blog}/{post_id}"
            return StoredChunkRef(adapter_name="tumblr", location=loc)
        finally:
            await page.close()

    async def _browser_read(self, ref: StoredChunkRef) -> str:
        post_id = ref.location.rsplit("/", 1)[-1]
        page, _ = await self._browser_page()
        try:
            await page.goto(f"https://{self._blog}.tumblr.com/post/{post_id}", wait_until="domcontentloaded")
            body = page.locator('.post-body, .post-content').first
            if await body.count() > 0:
                return await body.inner_text()
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
                f"{_API_BASE}/user/info",
                headers=self._headers(),
            ) as resp:
                return resp.status == 200
        except Exception:
            return False
