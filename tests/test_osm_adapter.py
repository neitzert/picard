"""Tests for the OpenStreetMap Notes adapter -- mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.osm_adapter import OsmAdapter
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


def _make_adapter(**kwargs) -> OsmAdapter:
    defaults: dict = dict(
        api_url="https://api.openstreetmap.org",
        username="testuser",
        password="testpass",
    )
    defaults.update(kwargs)
    return OsmAdapter(**defaults)


def _mock_session(response_mock):
    """Build an async-context-manager mock for aiohttp.ClientSession."""
    session = AsyncMock()
    session.post.return_value = response_mock
    session.get.return_value = response_mock

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm, session


@pytest.mark.asyncio
class TestOsmAdapter:
    def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "osm-notes"
        assert p.max_chunk_bytes == 4096
        assert p.persistent is True
        assert p.ordered is False
        assert p.rate_limit_per_min == 10

    def test_key_to_latlon_deterministic(self):
        lat1, lon1 = OsmAdapter._key_to_latlon("test-key")
        lat2, lon2 = OsmAdapter._key_to_latlon("test-key")
        assert lat1 == lat2
        assert lon1 == lon2

    def test_key_to_latlon_different_keys(self):
        r1 = OsmAdapter._key_to_latlon("key-a")
        r2 = OsmAdapter._key_to_latlon("key-b")
        assert r1 != r2

    def test_key_to_latlon_range(self):
        lat, lon = OsmAdapter._key_to_latlon("range-check")
        assert -90.0 <= lat <= 90.0
        assert -180.0 <= lon <= 180.0

    def test_auth_with_credentials(self):
        a = _make_adapter(username="u", password="p")
        auth = a._auth()
        assert auth is not None
        assert isinstance(auth, aiohttp.BasicAuth)

    def test_auth_without_credentials(self):
        a = _make_adapter(username=None, password=None)
        assert a._auth() is None

    async def test_write_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()

        resp = AsyncMock()
        resp.raise_for_status = MagicMock()
        resp.json = AsyncMock(return_value={"properties": {"id": 12345}})
        cm, session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=cm):
            ref = await a.write_chunk(enc)

        assert ref.adapter_name == "osm-notes"
        assert ref.location == "osm://notes/12345"
        assert ref.location in a._cache

    async def test_read_chunk_from_cache(self):
        a = _make_adapter()
        enc = _sample_encoded()
        loc = "osm://notes/99"
        a._cache[loc] = enc

        ref = StoredChunkRef(adapter_name="osm-notes", location=loc)
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_read_chunk_from_api(self):
        a = _make_adapter()
        enc = _sample_encoded()

        resp = AsyncMock()
        resp.raise_for_status = MagicMock()
        resp.json = AsyncMock(return_value={
            "properties": {
                "comments": [{"text": f"PICARD:key:{enc}"}]
            }
        })
        cm, session = _mock_session(resp)

        ref = StoredChunkRef(adapter_name="osm-notes", location="osm://notes/42")

        with patch("aiohttp.ClientSession", return_value=cm):
            result = await a.read_chunk(ref)

        assert result == enc

    async def test_delete_chunk_success(self):
        a = _make_adapter()
        enc = _sample_encoded()
        loc = "osm://notes/55"
        a._cache[loc] = enc

        resp = AsyncMock()
        resp.raise_for_status = MagicMock()
        cm, session = _mock_session(resp)

        ref = StoredChunkRef(adapter_name="osm-notes", location=loc)

        with patch("aiohttp.ClientSession", return_value=cm):
            result = await a.delete_chunk(ref)

        assert result is True
        assert loc not in a._cache

    async def test_delete_chunk_failure(self):
        a = _make_adapter()

        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=Exception("forbidden"))
        cm.__aexit__ = AsyncMock(return_value=False)

        ref = StoredChunkRef(adapter_name="osm-notes", location="osm://notes/99")

        with patch("aiohttp.ClientSession", return_value=cm):
            result = await a.delete_chunk(ref)

        assert result is False

    async def test_list_chunks(self):
        a = _make_adapter()
        a._cache["osm://notes/1"] = "data-with-m1"
        a._cache["osm://notes/2"] = "other-data"

        refs = await a.list_chunks("1")
        assert len(refs) == 1
        assert refs[0].location == "osm://notes/1"

    async def test_health_check_ok(self):
        a = _make_adapter()

        resp = AsyncMock()
        resp.status = 200
        cm, session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=cm):
            assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()

        resp = AsyncMock()
        resp.status = 503
        cm, session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=cm):
            assert await a.health_check() is False

    async def test_capacity(self):
        a = _make_adapter()
        enc = _sample_encoded()
        a._cache["osm://notes/1"] = enc

        cap = await a.capacity()
        assert cap["used_bytes"] == len(enc.encode())
        assert cap["free_bytes"] is None
        assert cap["total_bytes"] is None
        assert cap["chunk_count"] == 1
