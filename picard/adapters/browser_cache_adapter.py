"""Browser Cache API adapter -- stores chunks via the Service Worker Cache API.

Uses Playwright to drive a headless browser, navigating to a routed localhost
page to establish a secure context (required by the Cache API), then stores
chunks as Response objects keyed by URL path.

Requires: pip install playwright && playwright install chromium
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_LOCAL_URL = "http://localhost:19876/picard"

_WRITE_JS = """
async ({token, value}) => {
    const cache = await caches.open('picard-store');
    await cache.put(
        new Request('/chunk/' + token),
        new Response(value, {headers: {'Content-Type': 'text/plain'}})
    );
}
"""

_READ_JS = """
async (token) => {
    const cache = await caches.open('picard-store');
    const resp = await cache.match(new Request('/chunk/' + token));
    if (!resp) return null;
    return await resp.text();
}
"""

_DELETE_JS = """
async (token) => {
    const cache = await caches.open('picard-store');
    return await cache.delete(new Request('/chunk/' + token));
}
"""

_LIST_JS = """
async () => {
    const cache = await caches.open('picard-store');
    const keys = await cache.keys();
    const results = [];
    for (const req of keys) {
        const resp = await cache.match(req);
        const text = await resp.text();
        results.push({url: req.url, value: text});
    }
    return results;
}
"""

_HTML_STUB = "<html><head></head><body>PICARD</body></html>"


class BrowserCacheAdapter(Adapter):
    """Scatter chunks into the browser Cache API via Playwright."""

    def __init__(
        self,
        *,
        headless: bool = True,
        max_chunk_bytes: int = 10 * 1024 * 1024,
    ) -> None:
        self._headless = headless
        self._max = max_chunk_bytes
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="browser_cache",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=300,
            tags={
                "requires": "playwright,chromium",
                "substrate": "browser-cache-api",
            },
        )

    async def _ensure_page(self):
        if self._page is not None:
            return self._page
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise ImportError(
                "playwright is required: pip install playwright && playwright install chromium"
            ) from e
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self._headless)
        self._context = await self._browser.new_context()
        self._page = await self._context.new_page()

        async def _handle_route(route):
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=_HTML_STUB,
            )

        await self._page.route("**/*", _handle_route)
        await self._page.goto(_LOCAL_URL)
        return self._page

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        page = await self._ensure_page()
        token = uuid.uuid4().hex
        await page.evaluate(_WRITE_JS, {"token": token, "value": encoded_chunk})
        return StoredChunkRef(adapter_name="browser_cache", location=token)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        page = await self._ensure_page()
        result = await page.evaluate(_READ_JS, ref.location)
        if result is None:
            raise KeyError(f"chunk not found: {ref.location}")
        return result

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        page = await self._ensure_page()
        deleted = await page.evaluate(_DELETE_JS, ref.location)
        return bool(deleted)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        from picard.chunker import Chunk

        page = await self._ensure_page()
        entries = await page.evaluate(_LIST_JS)
        refs: list[StoredChunkRef] = []
        for entry in entries:
            token = entry["url"].rsplit("/chunk/", 1)[-1]
            try:
                if Chunk.decode(entry["value"]).manifest_id == manifest_id:
                    refs.append(StoredChunkRef(adapter_name="browser_cache", location=token))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            await self._ensure_page()
            return self._page is not None
        except Exception:
            return False

    async def close(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
            self._context = None
            self._page = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
