"""Tests for the MediaWiki adapter -- mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.mediawiki_adapter import MediaWikiAdapter
from picard.chunker import Chunk


def _sample_encoded(manifest_id: str = "m1", block: int = 0, idx: int = 0) -> str:
    return Chunk(
        version=1,
        manifest_id=manifest_id,
        block_index=block,
        chunk_index=idx,
        total_chunks=1,
        payload=b"test-payload",
    ).encode()


def _make_adapter(**kwargs) -> MediaWikiAdapter:
    defaults = dict(
        api_url="https://wiki.example.com/w/api.php",
        username="testuser",
        password="testpass",
    )
    defaults.update(kwargs)
    return MediaWikiAdapter(**defaults)


@pytest.mark.asyncio
class TestMediaWikiAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "mediawiki"
        assert p.max_chunk_bytes == 65536
        assert p.persistent is True
        assert p.ordered is False
        assert p.rate_limit_per_min == 10

    async def test_write_chunk(self):
        a = _make_adapter()
        a._login = AsyncMock(return_value="csrf-token-123")
        enc = _sample_encoded()

        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()

        mock_session = AsyncMock()
        mock_session.post = AsyncMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            ref = await a.write_chunk(enc)

        assert ref.adapter_name == "mediawiki"
        assert ref.location.startswith("wiki://")
        assert ref.location in a._cache
        assert a._cache[ref.location] == enc

    async def test_write_chunk_fallback_uuid(self):
        a = _make_adapter()
        a._login = AsyncMock(return_value="csrf-token-123")

        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()

        mock_session = AsyncMock()
        mock_session.post = AsyncMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            ref = await a.write_chunk("not-a-valid-chunk")

        assert ref.adapter_name == "mediawiki"
        assert ref.location.startswith("wiki://")

    async def test_read_chunk_from_cache(self):
        a = _make_adapter()
        loc = "wiki://https://wiki.example.com/w/api.php/User:testuser/picard/m1/0/0"
        a._cache[loc] = "cached-data"

        ref = StoredChunkRef(adapter_name="mediawiki", location=loc)
        result = await a.read_chunk(ref)
        assert result == "cached-data"

    async def test_read_chunk_from_api(self):
        a = _make_adapter()

        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={
            "parse": {"wikitext": {"*": "chunk-content-from-api"}}
        })

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            ref = StoredChunkRef(adapter_name="mediawiki", location="wiki://host/SomePage")
            result = await a.read_chunk(ref)

        assert result == "chunk-content-from-api"

    async def test_delete_chunk_success(self):
        a = _make_adapter()
        a._login = AsyncMock(return_value="csrf-token-123")
        loc = "wiki://host/SomePage"
        a._cache[loc] = "data"

        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()

        mock_session = AsyncMock()
        mock_session.post = AsyncMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            ref = StoredChunkRef(adapter_name="mediawiki", location=loc)
            result = await a.delete_chunk(ref)

        assert result is True
        assert loc not in a._cache

    async def test_delete_chunk_failure(self):
        a = _make_adapter()
        a._login = AsyncMock(side_effect=Exception("login failed"))

        ref = StoredChunkRef(adapter_name="mediawiki", location="wiki://host/SomePage")
        result = await a.delete_chunk(ref)
        assert result is False

    async def test_list_chunks_filters(self):
        a = _make_adapter()
        a._cache["wiki://host/m1/0/0"] = "data1"
        a._cache["wiki://host/m1/0/1"] = "data2"
        a._cache["wiki://host/m2/0/0"] = "data3"

        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert all(r.adapter_name == "mediawiki" for r in refs)

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        refs = await a.list_chunks("nonexistent")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()

        mock_resp = AsyncMock()
        mock_resp.status = 200

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(side_effect=Exception("connection refused"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            assert await a.health_check() is False

    async def test_capacity(self):
        a = _make_adapter()
        a._cache["loc1"] = "hello"
        a._cache["loc2"] = "world!"

        cap = await a.capacity()
        assert cap["used_bytes"] == len("hello".encode()) + len("world!".encode())
        assert cap["chunk_count"] == 2
        assert cap["free_bytes"] is None
        assert cap["total_bytes"] is None
