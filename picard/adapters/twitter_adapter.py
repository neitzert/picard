"""Twitter/X adapter — stores chunks as tweets or DMs.

Each chunk becomes a tweet or direct message:
    twitter://{tweet_id}  or  twitter://dm/{dm_id}

Two modes:
  - API mode (default): Twitter API v2 — requires bearer_token
  - Browser mode: Playwright — pip install picard[browser]
"""

from __future__ import annotations

from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_API = "https://api.twitter.com/2"
_WEB = "https://x.com"


class TwitterAdapter(Adapter):
    """Scatter chunks into Twitter/X tweets or DMs."""

    def __init__(
        self,
        bearer_token: str = "",
        *,
        access_token: str | None = None,
        access_secret: str | None = None,
        api_key: str | None = None,
        api_secret: str | None = None,
        username: str | None = None,
        password: str | None = None,
        use_dm: bool = False,
        dm_recipient_id: str | None = None,
        max_chunk_bytes: int | None = None,
        use_browser: bool = False,
        headless: bool = True,
    ) -> None:
        self._bearer = bearer_token
        self._access_token = access_token
        self._access_secret = access_secret
        self._api_key = api_key
        self._api_secret = api_secret
        self._username = username or ""
        self._password = password or ""
        self._use_dm = use_dm
        self._dm_recipient_id = dm_recipient_id
        self._max = max_chunk_bytes or (10_000 if use_dm else 280)
        self._use_browser = use_browser
        self._headless = headless

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="twitter",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=5 if self._use_browser else 15,
        )

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._bearer}",
            "Content-Type": "application/json",
        }

    # ── API mode ────────────────────────────────────────────────

    async def _api_write(self, encoded_chunk: str) -> StoredChunkRef:
        import aiohttp

        if self._use_dm and self._dm_recipient_id:
            body = {
                "text": encoded_chunk,
                "participant_id": self._dm_recipient_id,
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{_API}/dm_conversations/with/{self._dm_recipient_id}/messages",
                    json=body,
                    headers=self._headers(),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    dm_id = data["data"]["dm_event_id"]
            location = f"twitter://dm/{dm_id}"
        else:
            body = {"text": encoded_chunk}
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{_API}/tweets", json=body, headers=self._headers()
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    tweet_id = data["data"]["id"]
            location = f"twitter://{tweet_id}"

        return StoredChunkRef(adapter_name="twitter", location=location)

    async def _api_read(self, ref: StoredChunkRef) -> str:
        import aiohttp

        path = ref.location.split("://", 1)[1]

        if path.startswith("dm/"):
            dm_id = path.split("/", 1)[1]
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{_API}/dm_events/{dm_id}",
                    headers=self._headers(),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
            return data["data"]["text"]
        else:
            tweet_id = path
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{_API}/tweets/{tweet_id}",
                    headers=self._headers(),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
            return data["data"]["text"]

    async def _api_delete(self, ref: StoredChunkRef) -> bool:
        import aiohttp

        path = ref.location.split("://", 1)[1]
        if path.startswith("dm/"):
            return False

        tweet_id = path
        try:
            async with aiohttp.ClientSession() as session:
                async with session.delete(
                    f"{_API}/tweets/{tweet_id}", headers=self._headers()
                ) as resp:
                    data = await resp.json()
                    return data.get("data", {}).get("deleted", False)
        except Exception:
            return False

    async def _api_health(self) -> bool:
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{_API}/users/me", headers=self._headers()
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False

    # ── Browser mode ────────────────────────────────────────────

    async def _browser_page(self):
        from picard.browser_pool import BrowserPool

        pool = await BrowserPool.get(headless=self._headless)
        ctx = await pool.context("twitter")
        page = await ctx.new_page()

        await page.goto(f"{_WEB}/home", wait_until="domcontentloaded")
        logged_in = await page.locator(
            '[data-testid="SideNav_AccountSwitcher_Button"]'
        ).count() > 0

        if not logged_in:
            await page.goto(f"{_WEB}/i/flow/login", wait_until="domcontentloaded")
            await page.fill('input[autocomplete="username"]', self._username)
            await page.click('button:has-text("Next")')
            await page.wait_for_timeout(1000)
            await page.fill('input[type="password"]', self._password)
            await page.click('button[data-testid="LoginForm_Login_Button"]')
            await page.wait_for_url(f"{_WEB}/home", timeout=15000)
            await pool.save_state("twitter")

        return page, pool

    async def _browser_write(self, encoded_chunk: str) -> StoredChunkRef:
        page, pool = await self._browser_page()
        try:
            tweet_box = page.locator('[data-testid="tweetTextarea_0"]')
            await tweet_box.click()
            await tweet_box.fill(encoded_chunk)
            await page.click('[data-testid="tweetButtonInline"]')
            await page.wait_for_timeout(3000)

            await page.goto(
                f"{_WEB}/{self._username}", wait_until="domcontentloaded"
            )
            first_tweet = page.locator('article[data-testid="tweet"]').first
            link = first_tweet.locator('a[href*="/status/"]').first
            href = await link.get_attribute("href") or ""
            tweet_id = (
                href.split("/status/")[1].split("?")[0]
                if "/status/" in href
                else "unknown"
            )

            await pool.save_state("twitter")
            return StoredChunkRef(
                adapter_name="twitter", location=f"twitter://{tweet_id}"
            )
        finally:
            await page.close()

    async def _browser_read(self, ref: StoredChunkRef) -> str:
        path = ref.location.split("://", 1)[1]
        if path.startswith("dm/"):
            raise NotImplementedError("browser DM read not supported")
        tweet_id = path

        page, _ = await self._browser_page()
        try:
            await page.goto(
                f"{_WEB}/{self._username}/status/{tweet_id}",
                wait_until="domcontentloaded",
            )
            tweet_text = page.locator('[data-testid="tweetText"]').first
            return await tweet_text.inner_text()
        finally:
            await page.close()

    async def _browser_delete(self, ref: StoredChunkRef) -> bool:
        path = ref.location.split("://", 1)[1]
        if path.startswith("dm/"):
            return False
        tweet_id = path

        page, pool = await self._browser_page()
        try:
            await page.goto(
                f"{_WEB}/{self._username}/status/{tweet_id}",
                wait_until="domcontentloaded",
            )
            caret = page.locator('[data-testid="caret"]').first
            await caret.click()
            delete_btn = page.locator('[data-testid="Dropdown"] >> text=Delete')
            await delete_btn.click()
            confirm = page.locator('[data-testid="confirmationSheetConfirm"]')
            await confirm.click()
            await page.wait_for_timeout(2000)
            await pool.save_state("twitter")
            return True
        except Exception:
            return False
        finally:
            await page.close()

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
        return []

    async def health_check(self) -> bool:
        if self._use_browser:
            try:
                page, _ = await self._browser_page()
                await page.close()
                return True
            except Exception:
                return False
        return await self._api_health()
