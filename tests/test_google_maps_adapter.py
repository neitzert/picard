"""Tests for Google Maps Reviews adapter — uses in-memory mock."""

from __future__ import annotations

import json
import sys
import types
from unittest.mock import patch

import pytest

# Stub aiohttp
_aiohttp = types.ModuleType("aiohttp")
_aiohttp.ClientSession = type("ClientSession", (), {})
sys.modules.setdefault("aiohttp", _aiohttp)

from picard.adapters.google_maps_reviews import GoogleMapsAdapter


class _FakeResponse:
    def __init__(self, data, status=200):
        self._data = data
        self.status = status

    def raise_for_status(self):
        pass

    async def json(self):
        return self._data


class _FakeSession:
    def __init__(self):
        self._reviews: dict[str, str] = {}
        self._counter = 0

    async def post(self, url, **kwargs):
        self._counter += 1
        rid = f"review_{self._counter}"
        body = kwargs.get("json", {})
        self._reviews[rid] = body.get("text", "")
        return _FakeResponse({"review_id": rid})

    async def get(self, url, **kwargs):
        reviews = [
            {"text": text, "author_name": "test"}
            for text in self._reviews.values()
        ]
        return _FakeResponse({"result": {"reviews": reviews, "name": "Test Place"}})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass


@pytest.fixture
def adapter():
    return GoogleMapsAdapter(api_key="test-key", place_id="ChIJtest")


@pytest.fixture
def fake():
    return _FakeSession()


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "google-maps"

    def test_max(self, adapter):
        assert adapter.profile().max_chunk_bytes == 4096


class TestWriteRead:
    @pytest.mark.asyncio
    async def test_roundtrip(self, adapter, fake):
        with patch("aiohttp.ClientSession", return_value=fake):
            ref = await adapter.write_chunk("data123")
            assert ref.adapter_name == "google-maps"
            assert "gmaps://" in ref.location
            result = await adapter.read_chunk(ref)
            assert result == "data123"

    @pytest.mark.asyncio
    async def test_multiple(self, adapter, fake):
        with patch("aiohttp.ClientSession", return_value=fake):
            refs = [await adapter.write_chunk(f"c{i}") for i in range(3)]
            for i, ref in enumerate(refs):
                assert await adapter.read_chunk(ref) == f"c{i}"


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete(self, adapter, fake):
        with patch("aiohttp.ClientSession", return_value=fake):
            ref = await adapter.write_chunk("bye")
            assert await adapter.delete_chunk(ref) is True
            assert await adapter.delete_chunk(ref) is False


class TestList:
    @pytest.mark.asyncio
    async def test_list_chunks(self, adapter, fake):
        with patch("aiohttp.ClientSession", return_value=fake):
            await adapter.write_chunk("d1")
            chunks = await adapter.list_chunks("ChIJtest")
            assert len(chunks) >= 1


class TestHealth:
    @pytest.mark.asyncio
    async def test_healthy(self, adapter, fake):
        with patch("aiohttp.ClientSession", return_value=fake):
            assert await adapter.health_check() is True


class TestCapacity:
    @pytest.mark.asyncio
    async def test_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 0

    @pytest.mark.asyncio
    async def test_with_data(self, adapter, fake):
        with patch("aiohttp.ClientSession", return_value=fake):
            await adapter.write_chunk("payload")
            cap = await adapter.capacity()
            assert cap["chunk_count"] == 1
            assert cap["used_bytes"] > 0
