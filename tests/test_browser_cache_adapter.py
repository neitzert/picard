"""Unit tests for browser_cache_adapter (playwright mocked)."""
import asyncio
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from picard.adapters.browser_cache_adapter import BrowserCacheAdapter
from picard.adapters.base import StoredChunkRef

CHUNK = "hello"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_profile():
    a = BrowserCacheAdapter()
    p = a.profile()
    assert p.name == "browser_cache"


def test_no_playwright_raises():
    a = BrowserCacheAdapter()
    with patch.dict("sys.modules", {"playwright": None, "playwright.async_api": None}):
        with pytest.raises(Exception):
            _run(a.write_chunk(CHUNK))


def test_write_read_delete():
    a = BrowserCacheAdapter()
    store = {}

    async def _evaluate(js, arg=None):
        if arg and isinstance(arg, dict) and "token" in arg:
            store[arg["token"]] = arg["value"]
            return None
        if "delete" in js and arg and isinstance(arg, str):
            existed = arg in store
            store.pop(arg, None)
            return existed
        if "match" in js and arg and isinstance(arg, str):
            return store.get(arg)
        if "keys" in js:
            return [{"url": f"http://localhost/chunk/{k}", "value": v} for k, v in store.items()]
        return None

    mock_page = MagicMock()
    mock_page.evaluate = _evaluate
    mock_page.goto = AsyncMock()
    mock_page.route = AsyncMock()

    async def _ensure_page():
        a._page = mock_page
        return mock_page

    with patch.object(a, "_ensure_page", side_effect=_ensure_page):
        ref = _run(a.write_chunk(CHUNK))
    assert ref.adapter_name == "browser_cache"

    with patch.object(a, "_ensure_page", side_effect=_ensure_page):
        data = _run(a.read_chunk(ref))
    assert data == CHUNK

    with patch.object(a, "_ensure_page", side_effect=_ensure_page):
        deleted = _run(a.delete_chunk(ref))
    assert deleted is True
