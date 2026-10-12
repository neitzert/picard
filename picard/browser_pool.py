"""BrowserPool — singleton managing Playwright browser contexts.

Provides persistent browser sessions for adapters that need browser
automation (social platforms with no/limited API access).

Requires: pip install picard-store[browser]
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import ClassVar


class BrowserPool:
    """Manages a shared Playwright browser with per-adapter persistent contexts."""

    _instance: ClassVar[BrowserPool | None] = None

    def __init__(self, playwright, browser, headless: bool) -> None:
        self._playwright = playwright
        self._browser = browser
        self._headless = headless
        self._contexts: dict[str, object] = {}
        self._session_dir = Path(
            os.environ.get("PICARD_BROWSER_DIR", "~/.picard/browser_sessions")
        ).expanduser()

    @classmethod
    async def get(cls, *, headless: bool = True) -> BrowserPool:
        if cls._instance is not None:
            return cls._instance
        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=headless)
        pool = cls(pw, browser, headless)
        cls._instance = pool
        return pool

    async def context(self, adapter_name: str):
        if adapter_name in self._contexts:
            return self._contexts[adapter_name]

        storage_dir = self._session_dir / adapter_name
        storage_dir.mkdir(parents=True, exist_ok=True)
        state_file = storage_dir / "state.json"

        kwargs = {}
        if state_file.exists():
            kwargs["storage_state"] = str(state_file)

        ctx = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 720},
            **kwargs,
        )
        self._contexts[adapter_name] = ctx
        return ctx

    async def save_state(self, adapter_name: str) -> None:
        ctx = self._contexts.get(adapter_name)
        if ctx is None:
            return
        storage_dir = self._session_dir / adapter_name
        storage_dir.mkdir(parents=True, exist_ok=True)
        state_file = storage_dir / "state.json"
        await ctx.storage_state(path=str(state_file))

    async def close(self) -> None:
        for name in list(self._contexts):
            try:
                await self.save_state(name)
                await self._contexts[name].close()
            except Exception:
                pass
        self._contexts.clear()
        try:
            await self._browser.close()
        except Exception:
            pass
        try:
            await self._playwright.stop()
        except Exception:
            pass
        BrowserPool._instance = None
