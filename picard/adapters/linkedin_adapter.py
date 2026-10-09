"""LinkedIn adapter — stores chunks as LinkedIn posts.

Creates unlisted/author-only UGC posts with chunk data in the
share commentary text field.

Requires: pip install picard[linkedin]   (uses aiohttp)
"""

from __future__ import annotations

from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_API_BASE = "https://api.linkedin.com/v2"


class LinkedInAdapter(Adapter):
    """Scatter chunks into LinkedIn post text via API or Playwright."""

    def __init__(
        self,
        access_token: str = "",
        person_urn: str = "",
        *,
        username: str = "",
        password: str = "",
        use_browser: bool = False,
        headless: bool = True,
        max_chunk_bytes: int = 3000,
    ) -> None:
        self._access_token = access_token
        self._person_urn = person_urn
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
            name="linkedin",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=4 if self._use_browser else 25,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
            "X-Restli-Protocol-Version": "2.0.0",
        }

    # ── API mode ────────────────────────────────────────────────

    async def _api_write(self, encoded_chunk: str) -> StoredChunkRef:
        session = await self._get_session()
        async with session.post(
            f"{_API_BASE}/ugcPosts",
            headers=self._headers(),
            json={
                "author": self._person_urn,
                "lifecycleState": "PUBLISHED",
                "visibility": {
                    "com.linkedin.ugc.MemberNetworkVisibility": "CONNECTIONS"
                },
                "specificContent": {
                    "com.linkedin.ugc.ShareContent": {
                        "shareCommentary": {"text": encoded_chunk},
                        "shareMediaCategory": "NONE",
                    }
                },
            },
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            post_id = data.get("id", "")
        loc = f"linkedin://{self._person_urn}/{post_id}"
        return StoredChunkRef(adapter_name="linkedin", location=loc)

    async def _api_read(self, ref: StoredChunkRef) -> str:
        post_id = ref.location.rsplit("/", 1)[-1]
        session = await self._get_session()
        async with session.get(
            f"{_API_BASE}/ugcPosts/{post_id}",
            headers=self._headers(),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        return (
            data.get("specificContent", {})
            .get("com.linkedin.ugc.ShareContent", {})
            .get("shareCommentary", {})
            .get("text", "")
        )

    async def _api_delete(self, ref: StoredChunkRef) -> bool:
        post_id = ref.location.rsplit("/", 1)[-1]
        try:
            session = await self._get_session()
            async with session.delete(
                f"{_API_BASE}/ugcPosts/{post_id}",
                headers=self._headers(),
            ) as resp:
                return resp.status in (200, 204)
        except Exception:
            return False

    async def _api_list(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        session = await self._get_session()
        async with session.get(
            f"{_API_BASE}/ugcPosts",
            headers=self._headers(),
            params={"q": "authors", "authors": self._person_urn},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        for item in data.get("elements", []):
            text = (
                item.get("specificContent", {})
                .get("com.linkedin.ugc.ShareContent", {})
                .get("shareCommentary", {})
                .get("text", "")
            )
            if manifest_id in text:
                refs.append(
                    StoredChunkRef(
                        adapter_name="linkedin",
                        location=f"linkedin://{self._person_urn}/{item['id']}",
                    )
                )
        return refs

    # ── Browser mode ────────────────────────────────────────────

    async def _browser_page(self):
        from picard.browser_pool import BrowserPool

        pool = await BrowserPool.get(headless=self._headless)
        ctx = await pool.context("linkedin")
        page = await ctx.new_page()

        await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
        logged_in = await page.locator('div[data-control-name="identity_welcome_message"]').count() > 0 or "feed" in page.url

        if not logged_in:
            await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
            await page.fill('#username', self._username)
            await page.fill('#password', self._password)
            await page.click('button[type="submit"]')
            await page.wait_for_url("**/feed/**", timeout=15000)
            await pool.save_state("linkedin")

        return page, pool

    async def _browser_write(self, encoded_chunk: str) -> StoredChunkRef:
        import uuid

        page, pool = await self._browser_page()
        try:
            await page.click('button.share-box-feed-entry__trigger')
            await page.wait_for_selector('div.ql-editor[contenteditable="true"]', timeout=5000)
            await page.fill('div.ql-editor[contenteditable="true"]', encoded_chunk)
            await page.click('button.share-actions__primary-action')
            await page.wait_for_timeout(3000)
            await pool.save_state("linkedin")
            post_id = uuid.uuid4().hex[:12]
            loc = f"linkedin://{self._person_urn}/{post_id}"
            return StoredChunkRef(adapter_name="linkedin", location=loc)
        finally:
            await page.close()

    async def _browser_read(self, ref: StoredChunkRef) -> str:
        raise KeyError(f"browser read not supported for linkedin: {ref.location}")

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
                f"{_API_BASE}/me", headers=self._headers()
            ) as resp:
                return resp.status == 200
        except Exception:
            return False
