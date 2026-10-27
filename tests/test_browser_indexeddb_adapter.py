"""Unit tests for browser_indexeddb_adapter (playwright mocked)."""
import asyncio
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from picard.adapters.browser_indexeddb_adapter import BrowserIndexedDBAdapter
from picard.adapters.base import StoredChunkRef

CHUNK = "hello"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _mock_playwright():
    """Return a mock playwright context that records evaluate calls."""
    store = {}

    async def _evaluate(js, arg=None):
        if "put" in js or "readwrite" in js and arg and "token" in arg:
            store[arg["token"]] = arg["value"]
            return None
        if "get" in js and arg and isinstance(arg, str):
            return store.get(arg)
        if "delete" in js and arg and isinstance(arg, str):
            store.pop(arg, None)
            return None
        if "getAll" in js:
            return {"keys": list(store.keys()), "values": list(store.values())}
        return None

    mock_page = MagicMock()
    mock_page.evaluate = _evaluate
    mock_page.goto = AsyncMock()

    mock_context = MagicMock()
    mock_context.new_page = AsyncMock(return_value=mock_page)

    mock_browser = MagicMock()
    mock_browser.new_context = AsyncMock(return_value=mock_context)
    mock_browser.close = AsyncMock()

    mock_launcher = MagicMock()
    mock_launcher.launch = AsyncMock(return_value=mock_browser)

    mock_pw = MagicMock()
    mock_pw.chromium = mock_launcher
    mock_pw.stop = AsyncMock()

    mock_start = AsyncMock(return_value=mock_pw)

    mock_module = MagicMock()
    mock_module.async_playwright = MagicMock(return_value=MagicMock(__aenter__=AsyncMock(return_value=mock_pw)))

    async def _async_playwright_start():
        mock_ap = MagicMock()
        mock_ap.start = AsyncMock(return_value=mock_pw)
        return mock_ap

    return mock_module, mock_pw, mock_page, store


def test_profile():
    a = BrowserIndexedDBAdapter()
    p = a.profile()
    assert p.name == "browser_indexeddb"


def test_no_playwright_raises():
    a = BrowserIndexedDBAdapter()
    with patch.dict("sys.modules", {"playwright": None, "playwright.async_api": None}):
        with pytest.raises(Exception):
            _run(a.write_chunk(CHUNK))


def test_write_read_delete():
    a = BrowserIndexedDBAdapter()
    store = {}

    async def _evaluate(js, arg=None):
        if arg and isinstance(arg, dict) and "token" in arg:
            store[arg["token"]] = arg["value"]
            return None
        if arg and isinstance(arg, str):
            return store.get(arg)
        if "getAll" in js:
            return {"keys": list(store.keys()), "values": list(store.values())}
        return None

    mock_page = MagicMock()
    mock_page.evaluate = _evaluate
    mock_page.goto = AsyncMock()

    async def _ensure_page():
        a._page = mock_page
        return mock_page

    with patch.object(a, "_ensure_page", side_effect=_ensure_page):
        ref = _run(a.write_chunk(CHUNK))
    assert ref.adapter_name == "browser_indexeddb"

    with patch.object(a, "_ensure_page", side_effect=_ensure_page):
        data = _run(a.read_chunk(ref))
    assert data == CHUNK

    with patch.object(a, "_ensure_page", side_effect=_ensure_page):
        deleted = _run(a.delete_chunk(ref))
    assert deleted is True
