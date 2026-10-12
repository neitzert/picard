"""Tests for BrowserPool — lifecycle, singleton, context management."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.browser_pool import BrowserPool


@pytest.fixture(autouse=True)
def reset_singleton():
    BrowserPool._instance = None
    yield
    BrowserPool._instance = None


class TestBrowserPoolSingleton:
    @pytest.mark.asyncio
    async def test_get_returns_pool(self):
        mock_browser = AsyncMock()
        mock_pw = AsyncMock()
        mock_pw.chromium.launch = AsyncMock(return_value=mock_browser)
        mock_apw = MagicMock()
        mock_apw.return_value.start = AsyncMock(return_value=mock_pw)

        with patch.dict("sys.modules", {"playwright.async_api": MagicMock(async_playwright=mock_apw)}):
            pool = await BrowserPool.get(headless=True)
            assert pool is not None
            assert BrowserPool._instance is pool

    @pytest.mark.asyncio
    async def test_get_returns_same_instance(self):
        mock_browser = AsyncMock()
        mock_pw = AsyncMock()
        mock_pw.chromium.launch = AsyncMock(return_value=mock_browser)

        pool = BrowserPool(mock_pw, mock_browser, headless=True)
        BrowserPool._instance = pool

        pool2 = await BrowserPool.get()
        assert pool is pool2


class TestBrowserPoolContext:
    @pytest.mark.asyncio
    async def test_context_creates_new(self, tmp_path):
        mock_ctx = AsyncMock()
        mock_browser = AsyncMock()
        mock_browser.new_context = AsyncMock(return_value=mock_ctx)
        mock_pw = AsyncMock()

        pool = BrowserPool(mock_pw, mock_browser, headless=True)
        pool._session_dir = tmp_path / "sessions"

        ctx = await pool.context("test-adapter")
        assert ctx is mock_ctx
        mock_browser.new_context.assert_called_once()

    @pytest.mark.asyncio
    async def test_context_returns_cached(self, tmp_path):
        mock_ctx = AsyncMock()
        mock_browser = AsyncMock()
        mock_pw = AsyncMock()

        pool = BrowserPool(mock_pw, mock_browser, headless=True)
        pool._session_dir = tmp_path / "sessions"
        pool._contexts["test-adapter"] = mock_ctx

        ctx = await pool.context("test-adapter")
        assert ctx is mock_ctx
        mock_browser.new_context.assert_not_called()


class TestBrowserPoolClose:
    @pytest.mark.asyncio
    async def test_close_clears_state(self, tmp_path):
        mock_ctx = AsyncMock()
        mock_ctx.storage_state = AsyncMock()
        mock_ctx.close = AsyncMock()
        mock_browser = AsyncMock()
        mock_browser.close = AsyncMock()
        mock_pw = AsyncMock()
        mock_pw.stop = AsyncMock()

        pool = BrowserPool(mock_pw, mock_browser, headless=True)
        pool._session_dir = tmp_path / "sessions"
        pool._contexts["test"] = mock_ctx
        BrowserPool._instance = pool

        await pool.close()

        assert len(pool._contexts) == 0
        assert BrowserPool._instance is None
        mock_browser.close.assert_called_once()
        mock_pw.stop.assert_called_once()
