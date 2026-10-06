"""Tests for OSM Notes adapter — uses in-memory mock."""

from __future__ import annotations

import json
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Stub aiohttp before importing the adapter
_aiohttp = types.ModuleType("aiohttp")
_aiohttp.BasicAuth = MagicMock()
_aiohttp.ClientSession = MagicMock


class _FakeResponse:
    def __init__(self, data: dict, status: int = 200):
        self._data = data
        self.status = status

    def raise_for_status(self):
        pass

    async def json(self):
        return self._data

    async def text(self):
        return json.dumps(self._data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass


class _FakeSession:
    def __init__(self):
        self._note_counter = 0
        self._notes: dict[str, dict] = {}

    async def post(self, url, **kwargs):
        if "notes.json" in url and "close" not in url:
            self._note_counter += 1
            nid = str(self._note_counter)
            text = kwargs.get("params", {}).get("text", "")
            self._notes[nid] = {
                "properties": {
                    "id": int(nid),
                    "comments": [{"text": text}],
                }
            }
            return _FakeResponse(self._notes[nid]["properties"] | {"properties": self._notes[nid]["properties"]})
        if "close" in url:
            nid = url.split("/")[-2]
            self._notes.pop(nid, None)
            return _FakeResponse({})
        return _FakeResponse({})

    async def get(self, url, **kwargs):
        if "capabilities" in url:
            return _FakeResponse({}, 200)
        for nid, data in self._notes.items():
            if nid in url:
                return _FakeResponse(data)
        return _FakeResponse({}, 404)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass


sys.modules.setdefault("aiohttp", _aiohttp)

from picard.adapters.osm_adapter import OsmAdapter


@pytest.fixture
def adapter():
    return OsmAdapter(max_chunk_bytes=8192)


@pytest.fixture
def fake_session():
    return _FakeSession()


class TestProfile:
    def test_name(self, adapter):
        p = adapter.profile()
        assert p.name == "osm-notes"

    def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 8192

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 10


class TestWriteRead:
    @pytest.mark.asyncio
    async def test_roundtrip(self, adapter, fake_session):
        with patch("aiohttp.ClientSession", return_value=fake_session):
            ref = await adapter.write_chunk("testdata123")
            assert ref.adapter_name == "osm-notes"
            assert ref.location.startswith("osm://notes/")
            result = await adapter.read_chunk(ref)
            assert result == "testdata123"

    @pytest.mark.asyncio
    async def test_multiple_chunks(self, adapter, fake_session):
        with patch("aiohttp.ClientSession", return_value=fake_session):
            refs = []
            for i in range(3):
                refs.append(await adapter.write_chunk(f"chunk{i}"))
            for i, ref in enumerate(refs):
                assert await adapter.read_chunk(ref) == f"chunk{i}"


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete(self, adapter, fake_session):
        with patch("aiohttp.ClientSession", return_value=fake_session):
            ref = await adapter.write_chunk("todelete")
            result = await adapter.delete_chunk(ref)
            assert result is True


class TestListChunks:
    @pytest.mark.asyncio
    async def test_list(self, adapter, fake_session):
        with patch("aiohttp.ClientSession", return_value=fake_session):
            adapter._cache["osm://notes/1"] = "data"
            adapter._cache["osm://notes/2"] = "data"
            chunks = await adapter.list_chunks("notes")
            assert len(chunks) == 2


class TestHealth:
    @pytest.mark.asyncio
    async def test_healthy(self, adapter, fake_session):
        with patch("aiohttp.ClientSession", return_value=fake_session):
            assert await adapter.health_check() is True


class TestCapacity:
    @pytest.mark.asyncio
    async def test_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 0
        assert cap["used_bytes"] == 0

    @pytest.mark.asyncio
    async def test_with_data(self, adapter, fake_session):
        with patch("aiohttp.ClientSession", return_value=fake_session):
            await adapter.write_chunk("somedata")
            cap = await adapter.capacity()
            assert cap["chunk_count"] == 1
            assert cap["used_bytes"] > 0


class TestKeyToLatLon:
    def test_deterministic(self):
        a = OsmAdapter._key_to_latlon("key1")
        b = OsmAdapter._key_to_latlon("key1")
        assert a == b

    def test_different_keys(self):
        a = OsmAdapter._key_to_latlon("key1")
        b = OsmAdapter._key_to_latlon("key2")
        assert a != b

    def test_valid_range(self):
        lat, lon = OsmAdapter._key_to_latlon("test")
        assert -90 <= lat <= 90
        assert -180 <= lon <= 180
