"""Reddit adapter — stores chunks as self-posts on a subreddit.

Each chunk becomes a self-post whose body is the PCHK-encoded string.
Posts are titled with a manifest-id tag so they can be filtered for
recovery.

Two modes:
  - API mode (default): uses PRAW — pip install picard-store[reddit]
  - Browser mode: uses Playwright — pip install picard-store[browser]

Notice: This adapter must only be used with Reddit accounts and
subreddits you own and control.  Use in a manner that violates
Reddit's Terms of Service or API terms is outside the scope of this
project and the responsibility of the user.  No endorsement by or
affiliation with Reddit, Inc. is implied.
"""

from __future__ import annotations

import asyncio
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_OLD_REDDIT = "https://old.reddit.com"


class RedditAdapter(Adapter):
    """Scatter chunks into Reddit self-posts via PRAW or Playwright."""

    def __init__(
        self,
        client_id: str = "",
        client_secret: str = "",
        username: str = "",
        password: str = "",
        subreddit: str = "u_picard_test",
        *,
        user_agent: str = "picard/0.1",
        max_chunk_bytes: int = 39_000,
        use_browser: bool = False,
        headless: bool = True,
        max_posts: int = 10,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._username = username
        self._password = password
        self._subreddit_name = subreddit
        self._user_agent = user_agent
        self._max = max_chunk_bytes
        self._use_browser = use_browser
        self._headless = headless
        self._reddit: object | None = None
        self._max_posts = max_posts
        self._post_count = 0

    def _ensure_reddit(self):
        if self._reddit is not None:
            return self._reddit
        import praw

        self._reddit = praw.Reddit(
            client_id=self._client_id,
            client_secret=self._client_secret,
            user_agent=self._user_agent,
            username=self._username,
            password=self._password,
        )
        return self._reddit

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="reddit",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=4 if self._use_browser else 10,
        )

    # ── API mode ────────────────────────────────────────────────

    async def _api_write(self, encoded_chunk: str) -> StoredChunkRef:
        reddit = self._ensure_reddit()
        chunk = Chunk.decode(encoded_chunk)
        title = (
            f"[PCHK] {chunk.manifest_id} "
            f"b{chunk.block_index} c{chunk.chunk_index}"
        )

        def _submit():
            sub = reddit.subreddit(self._subreddit_name)
            submission = sub.submit(title=title, selftext=encoded_chunk)
            return submission.id

        submission_id = await asyncio.to_thread(_submit)
        return StoredChunkRef(adapter_name="reddit", location=submission_id)

    async def _api_read(self, ref: StoredChunkRef) -> str:
        reddit = self._ensure_reddit()

        def _read():
            return reddit.submission(id=ref.location).selftext

        return await asyncio.to_thread(_read)

    async def _api_delete(self, ref: StoredChunkRef) -> bool:
        reddit = self._ensure_reddit()

        def _delete():
            try:
                reddit.submission(id=ref.location).delete()
                return True
            except Exception:
                return False

        return await asyncio.to_thread(_delete)

    async def _api_list(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        reddit = self._ensure_reddit()
        tag = f"[PCHK] {manifest_id} "

        def _list():
            refs: list[StoredChunkRef] = []
            for submission in reddit.redditor(self._username).submissions.new(
                limit=1000
            ):
                if submission.title.startswith(tag):
                    refs.append(
                        StoredChunkRef(
                            adapter_name="reddit",
                            location=submission.id,
                        )
                    )
            return refs

        return await asyncio.to_thread(_list)

    # ── Browser mode ────────────────────────────────────────────

    async def _browser_page(self):
        from picard.browser_pool import BrowserPool

        pool = await BrowserPool.get(headless=self._headless)
        ctx = await pool.context("reddit")
        page = await ctx.new_page()

        # Check if already logged in
        await page.goto(f"{_OLD_REDDIT}/user/{self._username}", wait_until="domcontentloaded")
        logged_in = await page.locator("form.logout").count() > 0

        if not logged_in:
            await page.goto(f"{_OLD_REDDIT}/login", wait_until="domcontentloaded")
            await page.fill('input[name="user"]', self._username)
            await page.fill('input[name="passwd"]', self._password)
            await page.click('button[type="submit"]')
            await page.wait_for_url(f"{_OLD_REDDIT}/**", timeout=15000)
            await pool.save_state("reddit")

        return page, pool

    async def _browser_write(self, encoded_chunk: str) -> StoredChunkRef:
        chunk = Chunk.decode(encoded_chunk)
        title = (
            f"[PCHK] {chunk.manifest_id} "
            f"b{chunk.block_index} c{chunk.chunk_index}"
        )
        page, pool = await self._browser_page()
        try:
            await page.goto(
                f"{_OLD_REDDIT}/r/{self._subreddit_name}/submit?selftext=true",
                wait_until="domcontentloaded",
            )
            await page.fill('textarea[name="title"]', title)
            await page.fill('textarea[name="text"]', encoded_chunk)
            await page.click('button[name="submit"]')
            await page.wait_for_url("**/comments/**", timeout=30000)
            url = page.url
            submission_id = url.split("/comments/")[1].split("/")[0]
            await pool.save_state("reddit")
            return StoredChunkRef(adapter_name="reddit", location=submission_id)
        finally:
            await page.close()

    async def _browser_read(self, ref: StoredChunkRef) -> str:
        page, _ = await self._browser_page()
        try:
            await page.goto(
                f"{_OLD_REDDIT}/comments/{ref.location}",
                wait_until="domcontentloaded",
            )
            selftext = page.locator("div.usertext-body .md").first
            return await selftext.inner_text()
        finally:
            await page.close()

    async def _browser_delete(self, ref: StoredChunkRef) -> bool:
        page, pool = await self._browser_page()
        try:
            await page.goto(
                f"{_OLD_REDDIT}/comments/{ref.location}",
                wait_until="domcontentloaded",
            )
            delete_btn = page.locator('a.delete-button > span:text("delete")')
            if await delete_btn.count() == 0:
                return False
            await delete_btn.click()
            confirm = page.locator('.option.error a.yes')
            await confirm.click()
            await page.wait_for_timeout(2000)
            await pool.save_state("reddit")
            return True
        except Exception:
            return False
        finally:
            await page.close()

    async def _browser_list(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        page, _ = await self._browser_page()
        try:
            await page.goto(
                f"{_OLD_REDDIT}/user/{self._username}/submitted",
                wait_until="domcontentloaded",
            )
            refs: list[StoredChunkRef] = []
            tag = f"[PCHK] {manifest_id} "
            links = page.locator("a.title")
            count = await links.count()
            for i in range(count):
                text = await links.nth(i).inner_text()
                if text.startswith(tag):
                    href = await links.nth(i).get_attribute("href") or ""
                    if "/comments/" in href:
                        sid = href.split("/comments/")[1].split("/")[0]
                        refs.append(
                            StoredChunkRef(adapter_name="reddit", location=sid)
                        )
            return refs
        finally:
            await page.close()

    # ── Dispatch ────────────────────────────────────────────────

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        if self._post_count >= self._max_posts:
            raise RuntimeError(
                f"Reddit session post cap reached ({self._max_posts}); "
                "raise max_posts or use a different account"
            )
        if self._use_browser:
            result = await self._browser_write(encoded_chunk)
        else:
            result = await self._api_write(encoded_chunk)
        self._post_count += 1
        return result

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
            reddit = self._ensure_reddit()
            await asyncio.to_thread(lambda: reddit.user.me())
            return True
        except Exception:
            return False
