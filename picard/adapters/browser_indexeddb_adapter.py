"""Browser IndexedDB adapter -- stores chunks in browser IndexedDB via Playwright.

Each chunk is a record in the `picard-store` database, object store `chunks`,
keyed by a UUID token.  The database persists across page reloads within the
same browser context.

Requires: pip install playwright && playwright install chromium
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


_DB_SETUP_JS = """
async () => {
    await new Promise((res, rej) => {
        const req = indexedDB.open('picard-store', 1);
        req.onupgradeneeded = e => e.target.result.createObjectStore('chunks');
        req.onsuccess = () => res();
        req.onerror = e => rej(e.target.error);
    });
}
"""

_WRITE_JS = """
async ({token, value}) => {
    await new Promise((res, rej) => {
        const req = indexedDB.open('picard-store', 1);
        req.onsuccess = e => {
            const tx = e.target.result.transaction('chunks', 'readwrite');
            tx.objectStore('chunks').put(value, token);
            tx.oncomplete = res;
            tx.onerror = ev => rej(ev.target.error);
        };
        req.onerror = e => rej(e.target.error);
    });
}
"""

_READ_JS = """
async (token) => {
    return await new Promise((res, rej) => {
        const req = indexedDB.open('picard-store', 1);
        req.onsuccess = e => {
            const tx = e.target.result.transaction('chunks', 'readonly');
            const r = tx.objectStore('chunks').get(token);
            r.onsuccess = ev => res(ev.target.result ?? null);
            r.onerror = ev => rej(ev.target.error);
        };
        req.onerror = e => rej(e.target.error);
    });
}
"""

_DELETE_JS = """
async (token) => {
    await new Promise((res, rej) => {
        const req = indexedDB.open('picard-store', 1);
        req.onsuccess = e => {
            const tx = e.target.result.transaction('chunks', 'readwrite');
            tx.objectStore('chunks').delete(token);
            tx.oncomplete = res;
            tx.onerror = ev => rej(ev.target.error);
        };
        req.onerror = e => rej(e.target.error);
    });
}
"""

_GETALL_JS = """
async () => {
    return await new Promise((res, rej) => {
        const req = indexedDB.open('picard-store', 1);
        req.onsuccess = e => {
            const tx = e.target.result.transaction('chunks', 'readonly');
            const store = tx.objectStore('chunks');
            const keys = store.getAllKeys();
            const vals = store.getAll();
            let k, v;
            keys.onsuccess = ev => { k = ev.target.result; if (v) res({keys: k, values: v}); };
            vals.onsuccess = ev => { v = ev.target.result; if (k) res({keys: k, values: v}); };
            keys.onerror = vals.onerror = ev => rej(ev.target.error);
        };
        req.onerror = e => rej(e.target.error);
    });
}
"""


class BrowserIndexedDBAdapter(Adapter):
    """Scatter chunks into browser IndexedDB via Playwright."""

    def __init__(
        self,
        *,
        browser: str = "chromium",
        headless: bool = True,
        max_chunk_bytes: int = 10 * 1024 * 1024,
    ) -> None:
        self._browser_type = browser
        self._headless = headless
        self._max = max_chunk_bytes
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="browser_indexeddb",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=300,
            tags={
                "requires": "playwright,chromium",
                "substrate": "browser-indexeddb",
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
        launcher = getattr(self._playwright, self._browser_type)
        self._browser = await launcher.launch(headless=self._headless)
        self._context = await self._browser.new_context()
        self._page = await self._context.new_page()
        await self._page.goto("about:blank")
        await self._page.evaluate(_DB_SETUP_JS)
        return self._page

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        page = await self._ensure_page()
        token = uuid.uuid4().hex
        await page.evaluate(_WRITE_JS, {"token": token, "value": encoded_chunk})
        return StoredChunkRef(adapter_name="browser_indexeddb", location=token)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        page = await self._ensure_page()
        result = await page.evaluate(_READ_JS, ref.location)
        if result is None:
            raise KeyError(f"chunk not found: {ref.location}")
        return result

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        page = await self._ensure_page()
        await page.evaluate(_DELETE_JS, ref.location)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        from picard.chunker import Chunk

        page = await self._ensure_page()
        result = await page.evaluate(_GETALL_JS)
        refs: list[StoredChunkRef] = []
        for token, value in zip(result["keys"], result["values"]):
            try:
                if Chunk.decode(value).manifest_id == manifest_id:
                    refs.append(StoredChunkRef(adapter_name="browser_indexeddb", location=token))
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
