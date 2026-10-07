"""Tests for the Google Maps Reviews adapter -- mocked, no real API calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.google_maps_reviews import GoogleMapsAdapter
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


def _make_adapter(**kwargs) -> GoogleMapsAdapter:
    defaults = dict(
        api_key="test-key",
        place_id="test-place-id",
    )
    defaults.update(kwargs)
    return GoogleMapsAdapter(**defaults)


def _mock_response(status=200, json_data=None, text_data=""):
    resp = AsyncMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    resp.json = AsyncMock(return_value=json_data or {})
    resp.text = AsyncMock(return_value=text_data)
    return resp


@pytest.mark.asyncio
class TestGoogleMapsAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "google-maps"
        assert p.max_chunk_bytes == 4096
        assert p.persistent is True
        assert p.ordered is False
        assert p.rate_limit_per_min == 10

    async def test_profile_custom_max(self):
        a = _make_adapter(max_chunk_bytes=2048)
        p = a.profile()
        assert p.max_chunk_bytes == 2048

    async def test_write_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        resp = _mock_response(200, json_data={"review_id": "rev-123"})

        session = AsyncMock()
        session.post = AsyncMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("picard.adapters.google_maps_reviews.aiohttp.ClientSession", return_value=session):
            ref = await a.write_chunk(enc)

        assert ref.adapter_name == "google-maps"
        assert "gmaps://" in ref.location
        assert "test-place-id" in ref.location
        session.post.assert_called_once()

    async def test_write_chunk_populates_cache(self):
        a = _make_adapter()
        enc = _sample_encoded()
        resp = _mock_response(200, json_data={"review_id": "rev-456"})

        session = AsyncMock()
        session.post = AsyncMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("picard.adapters.google_maps_reviews.aiohttp.ClientSession", return_value=session):
            ref = await a.write_chunk(enc)

        assert ref.location in a._cache
        assert a._cache[ref.location] == enc

    async def test_read_chunk_from_cache(self):
        a = _make_adapter()
        enc = _sample_encoded()
        loc = "gmaps://test-place-id/rev-123"
        a._cache[loc] = enc

        ref = StoredChunkRef(adapter_name="google-maps", location=loc)
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_read_chunk_from_api(self):
        a = _make_adapter()
        enc = _sample_encoded()
        loc = "gmaps://test-place-id/rev-789"
        # The review text must contain "PICARD:" and the review_id "rev-789"
        review_text = f"PICARD:rev-789:{enc}"

        resp = _mock_response(200, json_data={
            "result": {
                "reviews": [
                    {"text": review_text},
                ]
            }
        })

        session = AsyncMock()
        session.get = AsyncMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        ref = StoredChunkRef(adapter_name="google-maps", location=loc)

        with patch("picard.adapters.google_maps_reviews.aiohttp.ClientSession", return_value=session):
            result = await a.read_chunk(ref)

        assert result == enc

    async def test_delete_chunk_cached(self):
        a = _make_adapter()
        loc = "gmaps://test-place-id/rev-123"
        a._cache[loc] = "some-data"

        ref = StoredChunkRef(adapter_name="google-maps", location=loc)
        result = await a.delete_chunk(ref)
        assert result is True
        assert loc not in a._cache

    async def test_delete_chunk_not_cached(self):
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="google-maps", location="gmaps://test-place-id/nonexistent")
        result = await a.delete_chunk(ref)
        assert result is False

    async def test_list_chunks(self):
        a = _make_adapter()
        a._cache["gmaps://test-place-id/rev-1"] = "data1"
        a._cache["gmaps://test-place-id/rev-2"] = "data2"
        a._cache["gmaps://other-place/rev-3"] = "data3"

        refs = await a.list_chunks("test-place-id")
        assert len(refs) == 2
        assert all(r.adapter_name == "google-maps" for r in refs)

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        refs = await a.list_chunks("nonexistent")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        resp = _mock_response(200)

        session = AsyncMock()
        session.get = AsyncMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("picard.adapters.google_maps_reviews.aiohttp.ClientSession", return_value=session):
            result = await a.health_check()

        assert result is True

    async def test_health_check_fail(self):
        a = _make_adapter()

        session = AsyncMock()
        session.get = AsyncMock(side_effect=Exception("network error"))
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("picard.adapters.google_maps_reviews.aiohttp.ClientSession", return_value=session):
            result = await a.health_check()

        assert result is False

    async def test_capacity(self):
        a = _make_adapter()
        a._cache["loc1"] = "hello"
        a._cache["loc2"] = "world!"

        cap = await a.capacity()
        assert cap["chunk_count"] == 2
        assert cap["used_bytes"] == len(b"hello") + len(b"world!")
        assert cap["free_bytes"] is None
        assert cap["total_bytes"] is None

    async def test_capacity_empty(self):
        a = _make_adapter()
        cap = await a.capacity()
        assert cap["chunk_count"] == 0
        assert cap["used_bytes"] == 0
