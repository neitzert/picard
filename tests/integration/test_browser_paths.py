"""Integration tests for adapter browser fallback paths.

Each test exercises the Playwright-backed write/read/delete path of a
dual-mode adapter.  All tests are skipped automatically when the required
environment variables are not set, so the suite is safe to run in CI
without credentials.

Set credentials before running:

  export PICARD_TEST_TWITTER_USERNAME=...
  export PICARD_TEST_TWITTER_PASSWORD=...
  # ... etc. for each platform

Run only these tests:

  pytest tests/integration/ -m "integration and browser" --tb=short -v

Each test writes a uniquely-tagged chunk, reads it back where the adapter
supports it, and always attempts cleanup in a finally block.
"""

from __future__ import annotations

import base64
import os
import time

import pytest

from picard.adapters.base import StoredChunkRef


# ── helpers ─────────────────────────────────────────────────────────────────

def _creds(*env_keys: str) -> tuple[str, ...] | None:
    """Return env var values as a tuple, or None if any are missing."""
    values = [os.environ.get(k, "") for k in env_keys]
    return tuple(values) if all(values) else None


def _test_chunk(platform: str) -> str:
    tag = f"PICARD_INTEGRATION_TEST_{platform.upper()}_{int(time.time())}"
    return base64.b64encode(tag.encode()).decode()


# ── Twitter ──────────────────────────────────────────────────────────────────

@pytest.mark.integration
@pytest.mark.browser
@pytest.mark.asyncio
async def test_twitter_browser_write_read():
    creds = _creds("PICARD_TEST_TWITTER_USERNAME", "PICARD_TEST_TWITTER_PASSWORD")
    if not creds:
        pytest.skip("PICARD_TEST_TWITTER_USERNAME/PASSWORD not set")
    username, password = creds

    from picard.adapters.twitter_adapter import TwitterAdapter

    chunk = _test_chunk("twitter")
    adapter = TwitterAdapter(
        "", username=username, password=password, use_browser=True, headless=True
    )
    ref: StoredChunkRef | None = None
    try:
        ref = await adapter.write_chunk(chunk)
        assert isinstance(ref, StoredChunkRef)
        assert ref.adapter_name == "twitter"

        result = await adapter.read_chunk(ref)
        assert chunk in result
    finally:
        if ref is not None:
            await adapter.delete_chunk(ref)
        await adapter.close()


# ── Reddit ───────────────────────────────────────────────────────────────────

@pytest.mark.integration
@pytest.mark.browser
@pytest.mark.asyncio
async def test_reddit_browser_write_read_delete():
    creds = _creds("PICARD_TEST_REDDIT_USERNAME", "PICARD_TEST_REDDIT_PASSWORD")
    if not creds:
        pytest.skip("PICARD_TEST_REDDIT_USERNAME/PASSWORD not set")
    username, password = creds
    subreddit = os.environ.get("PICARD_TEST_REDDIT_SUBREDDIT", f"u_{username}")

    from picard.adapters.reddit import RedditAdapter

    chunk = _test_chunk("reddit")
    adapter = RedditAdapter(
        client_id="",
        client_secret="",
        username=username,
        password=password,
        subreddit=subreddit,
        use_browser=True,
        headless=True,
    )
    ref: StoredChunkRef | None = None
    try:
        ref = await adapter.write_chunk(chunk)
        assert isinstance(ref, StoredChunkRef)
        assert ref.adapter_name == "reddit"

        result = await adapter.read_chunk(ref)
        assert chunk in result

        deleted = await adapter.delete_chunk(ref)
        assert deleted is True
        ref = None
    finally:
        if ref is not None:
            await adapter.delete_chunk(ref)
        await adapter.close()


# ── Imgur ────────────────────────────────────────────────────────────────────

@pytest.mark.integration
@pytest.mark.browser
@pytest.mark.asyncio
async def test_imgur_browser_write():
    """Browser write-only: Imgur browser path returns a placeholder location."""
    creds = _creds("PICARD_TEST_IMGUR_USERNAME", "PICARD_TEST_IMGUR_PASSWORD")
    if not creds:
        pytest.skip("PICARD_TEST_IMGUR_USERNAME/PASSWORD not set")
    username, password = creds

    from picard.adapters.imgur_adapter import ImgurAdapter

    chunk = _test_chunk("imgur")
    adapter = ImgurAdapter(
        "", username=username, password=password, use_browser=True, headless=True
    )
    ref: StoredChunkRef | None = None
    try:
        ref = await adapter.write_chunk(chunk)
        assert isinstance(ref, StoredChunkRef)
        assert ref.adapter_name == "imgur"
        # Browser delete is not supported (returns False); that's expected
        deleted = await adapter.delete_chunk(ref)
        assert deleted is False
        ref = None
    finally:
        if ref is not None:
            await adapter.delete_chunk(ref)
        await adapter.close()


# ── Medium ───────────────────────────────────────────────────────────────────

@pytest.mark.integration
@pytest.mark.browser
@pytest.mark.asyncio
async def test_medium_browser_write_read_delete():
    """Medium browser path stores reads in an in-memory cache."""
    creds = _creds("PICARD_TEST_MEDIUM_USERNAME", "PICARD_TEST_MEDIUM_PASSWORD")
    if not creds:
        pytest.skip("PICARD_TEST_MEDIUM_USERNAME/PASSWORD not set")
    username, password = creds

    from picard.adapters.medium_adapter import MediumAdapter

    chunk = _test_chunk("medium")
    adapter = MediumAdapter(
        "", username=username, password=password, use_browser=True, headless=True
    )
    ref: StoredChunkRef | None = None
    try:
        ref = await adapter.write_chunk(chunk)
        assert isinstance(ref, StoredChunkRef)
        assert ref.adapter_name == "medium"

        result = await adapter.read_chunk(ref)
        assert result == chunk

        deleted = await adapter.delete_chunk(ref)
        assert deleted is True
        ref = None
    finally:
        if ref is not None:
            await adapter.delete_chunk(ref)
        await adapter.close()


# ── LinkedIn ─────────────────────────────────────────────────────────────────

@pytest.mark.integration
@pytest.mark.browser
@pytest.mark.asyncio
async def test_linkedin_browser_write_only():
    """LinkedIn browser path is write-only; read raises KeyError."""
    creds = _creds("PICARD_TEST_LINKEDIN_USERNAME", "PICARD_TEST_LINKEDIN_PASSWORD")
    if not creds:
        pytest.skip("PICARD_TEST_LINKEDIN_USERNAME/PASSWORD not set")
    username, password = creds

    from picard.adapters.linkedin_adapter import LinkedInAdapter

    chunk = _test_chunk("linkedin")
    adapter = LinkedInAdapter(
        username=username, password=password, use_browser=True, headless=True
    )
    ref: StoredChunkRef | None = None
    try:
        ref = await adapter.write_chunk(chunk)
        assert isinstance(ref, StoredChunkRef)
        assert ref.adapter_name == "linkedin"

        with pytest.raises(KeyError):
            await adapter.read_chunk(ref)

        deleted = await adapter.delete_chunk(ref)
        assert deleted is False
        ref = None
    finally:
        if ref is not None:
            await adapter.delete_chunk(ref)
        await adapter.close()


# ── Steam ────────────────────────────────────────────────────────────────────

@pytest.mark.integration
@pytest.mark.browser
@pytest.mark.asyncio
async def test_steam_browser_write_read_delete():
    """Steam browser path stores reads in an in-memory cache."""
    creds = _creds("PICARD_TEST_STEAM_USERNAME", "PICARD_TEST_STEAM_PASSWORD")
    if not creds:
        pytest.skip("PICARD_TEST_STEAM_USERNAME/PASSWORD not set")
    username, password = creds

    from picard.adapters.steam_adapter import SteamAdapter

    chunk = _test_chunk("steam")
    adapter = SteamAdapter(
        username=username, password=password, use_browser=True, headless=True
    )
    ref: StoredChunkRef | None = None
    try:
        ref = await adapter.write_chunk(chunk)
        assert isinstance(ref, StoredChunkRef)
        assert ref.adapter_name == "steam"

        result = await adapter.read_chunk(ref)
        assert result == chunk

        deleted = await adapter.delete_chunk(ref)
        assert deleted is True
        ref = None
    finally:
        if ref is not None:
            await adapter.delete_chunk(ref)
        await adapter.close()


# ── Twitch ───────────────────────────────────────────────────────────────────

@pytest.mark.integration
@pytest.mark.browser
@pytest.mark.asyncio
async def test_twitch_browser_write_read():
    """Twitch browser path writes to channel description; read fetches it back."""
    creds = _creds("PICARD_TEST_TWITCH_USERNAME", "PICARD_TEST_TWITCH_PASSWORD")
    if not creds:
        pytest.skip("PICARD_TEST_TWITCH_USERNAME/PASSWORD not set")
    username, password = creds

    from picard.adapters.twitch_adapter import TwitchAdapter

    chunk = _test_chunk("twitch")
    adapter = TwitchAdapter(
        username=username, password=password, use_browser=True, headless=True
    )
    ref: StoredChunkRef | None = None
    try:
        ref = await adapter.write_chunk(chunk)
        assert isinstance(ref, StoredChunkRef)
        assert ref.adapter_name == "twitch"

        result = await adapter.read_chunk(ref)
        assert chunk in result

        # Channel description delete is not supported in browser mode
        deleted = await adapter.delete_chunk(ref)
        assert deleted is False
        ref = None
    finally:
        if ref is not None:
            await adapter.delete_chunk(ref)
        await adapter.close()


# ── WordPress ────────────────────────────────────────────────────────────────

@pytest.mark.integration
@pytest.mark.browser
@pytest.mark.asyncio
async def test_wordpress_browser_write():
    """WordPress browser path writes a draft post; read requires a real post ID."""
    creds = _creds(
        "PICARD_TEST_WORDPRESS_USERNAME",
        "PICARD_TEST_WORDPRESS_PASSWORD",
        "PICARD_TEST_WORDPRESS_SITE_URL",
    )
    if not creds:
        pytest.skip(
            "PICARD_TEST_WORDPRESS_USERNAME/PASSWORD/SITE_URL not set"
        )
    username, password, site_url = creds

    from picard.adapters.wordpress_adapter import WordPressAdapter

    chunk = _test_chunk("wordpress")
    adapter = WordPressAdapter(
        site_url=site_url,
        username=username,
        password=password,
        use_browser=True,
        headless=True,
    )
    ref: StoredChunkRef | None = None
    try:
        ref = await adapter.write_chunk(chunk)
        assert isinstance(ref, StoredChunkRef)
        assert ref.adapter_name == "wordpress"
        # Browser write returns a placeholder post ID; skip round-trip read
        deleted = await adapter.delete_chunk(ref)
        assert deleted is False
        ref = None
    finally:
        if ref is not None:
            await adapter.delete_chunk(ref)
        await adapter.close()


# ── Tumblr ───────────────────────────────────────────────────────────────────

@pytest.mark.integration
@pytest.mark.browser
@pytest.mark.asyncio
async def test_tumblr_browser_write():
    """Tumblr browser path writes a draft post; read requires a real post ID."""
    creds = _creds("PICARD_TEST_TUMBLR_USERNAME", "PICARD_TEST_TUMBLR_PASSWORD")
    if not creds:
        pytest.skip("PICARD_TEST_TUMBLR_USERNAME/PASSWORD not set")
    username, password = creds
    blog = os.environ.get("PICARD_TEST_TUMBLR_BLOG", username)

    from picard.adapters.tumblr_adapter import TumblrAdapter

    chunk = _test_chunk("tumblr")
    adapter = TumblrAdapter(
        blog_name=blog,
        username=username,
        password=password,
        use_browser=True,
        headless=True,
    )
    ref: StoredChunkRef | None = None
    try:
        ref = await adapter.write_chunk(chunk)
        assert isinstance(ref, StoredChunkRef)
        assert ref.adapter_name == "tumblr"
        # Browser write returns a placeholder post ID; skip round-trip read
        deleted = await adapter.delete_chunk(ref)
        assert deleted is False
        ref = None
    finally:
        if ref is not None:
            await adapter.delete_chunk(ref)
        await adapter.close()


# ── TikTok ───────────────────────────────────────────────────────────────────

@pytest.mark.integration
@pytest.mark.browser
@pytest.mark.asyncio
async def test_tiktok_browser_write_read_delete():
    """TikTok browser path stores reads in an in-memory cache."""
    creds = _creds("PICARD_TEST_TIKTOK_USERNAME", "PICARD_TEST_TIKTOK_PASSWORD")
    if not creds:
        pytest.skip("PICARD_TEST_TIKTOK_USERNAME/PASSWORD not set")
    username, password = creds

    from picard.adapters.tiktok_adapter import TikTokAdapter

    chunk = _test_chunk("tiktok")
    adapter = TikTokAdapter(
        username=username, password=password, use_browser=True, headless=True
    )
    ref: StoredChunkRef | None = None
    try:
        ref = await adapter.write_chunk(chunk)
        assert isinstance(ref, StoredChunkRef)
        assert ref.adapter_name == "tiktok"

        result = await adapter.read_chunk(ref)
        assert result == chunk

        deleted = await adapter.delete_chunk(ref)
        assert deleted is True
        ref = None
    finally:
        if ref is not None:
            await adapter.delete_chunk(ref)
        await adapter.close()


# ── Pinterest ────────────────────────────────────────────────────────────────

@pytest.mark.integration
@pytest.mark.browser
@pytest.mark.asyncio
async def test_pinterest_browser_write():
    """Pinterest browser path writes a pin; read requires the real pin URL."""
    creds = _creds("PICARD_TEST_PINTEREST_USERNAME", "PICARD_TEST_PINTEREST_PASSWORD")
    if not creds:
        pytest.skip("PICARD_TEST_PINTEREST_USERNAME/PASSWORD not set")
    username, password = creds

    from picard.adapters.pinterest_adapter import PinterestAdapter

    chunk = _test_chunk("pinterest")
    adapter = PinterestAdapter(
        username=username, password=password, use_browser=True, headless=True
    )
    ref: StoredChunkRef | None = None
    try:
        ref = await adapter.write_chunk(chunk)
        assert isinstance(ref, StoredChunkRef)
        assert ref.adapter_name == "pinterest"
        # Browser write returns a placeholder pin ID; skip round-trip read
        deleted = await adapter.delete_chunk(ref)
        assert deleted is False
        ref = None
    finally:
        if ref is not None:
            await adapter.delete_chunk(ref)
        await adapter.close()
