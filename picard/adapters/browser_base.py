"""Browser adapter base — abstract base for Playwright-driven adapters.

Subclasses implement _login / _is_logged_in / the browser-specific
write/read/delete logic.  The base handles session lifecycle via
BrowserPool.

Not auto-discovered by the registry (filtered in registry.py).
"""

from __future__ import annotations

from abc import abstractmethod

from picard.adapters.base import Adapter


class BrowserAdapter(Adapter):
    """Abstract base for adapters that use Playwright browser automation."""

    def __init__(self, *, headless: bool = True, **kwargs) -> None:
        self._headless = headless
        self._pool = None

    async def _get_pool(self):
        if self._pool is None:
            from picard.browser_pool import BrowserPool
            self._pool = await BrowserPool.get(headless=self._headless)
        return self._pool

    async def _page(self, adapter_name: str):
        pool = await self._get_pool()
        ctx = await pool.context(adapter_name)
        page = await ctx.new_page()
        if not await self._is_logged_in(page):
            await self._login(page)
            await pool.save_state(adapter_name)
        return page

    @abstractmethod
    async def _login(self, page) -> None:
        """Authenticate on the target platform."""

    @abstractmethod
    async def _is_logged_in(self, page) -> bool:
        """Return True if the current session is authenticated."""

    async def health_check(self) -> bool:
        try:
            pool = await self._get_pool()
            return pool._browser.is_connected()
        except Exception:
            return False
