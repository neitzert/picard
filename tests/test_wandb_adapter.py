"""Tests for Weights & Biases adapter — uses in-memory mock."""

from __future__ import annotations

import sys
import types
from unittest.mock import patch

import pytest

_aiohttp = types.ModuleType("aiohttp")
_aiohttp.ClientSession = type("ClientSession", (), {})
sys.modules.setdefault("aiohttp", _aiohttp)

from picard.adapters.wandb_adapter import WandbAdapter


class _FakeResponse:
    def __init__(self, data=None, status=200):
        self._data = data or {}
        self.status = status

    def raise_for_status(self):
        pass

    async def json(self):
        return self._data


class _FakeSession:
    def __init__(self):
        self._counter = 0
        self._artifacts: dict[str, str] = {}

    async def post(self, url, **kwargs):
        body = kwargs.get("json", {})
        query = body.get("query", "")
        if "CreateArtifact" in query:
            self._counter += 1
            aid = f"art_{self._counter}"
            desc = body.get("variables", {}).get("description", "")
            self._artifacts[aid] = desc
            return _FakeResponse({
                "data": {"createArtifact": {"artifact": {"id": aid}}}
            })
        if "GetArtifact" in query:
            aid = body.get("variables", {}).get("id", "")
            return _FakeResponse({
                "data": {"artifact": {"description": self._artifacts.get(aid, "")}}
            })
        if "DeleteArtifact" in query:
            return _FakeResponse({"data": {"deleteArtifact": {"success": True}}})
        return _FakeResponse({})

    async def get(self, url, **kwargs):
        if "healthz" in url:
            return _FakeResponse(status=200)
        return _FakeResponse(status=404)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass


@pytest.fixture
def adapter():
    return WandbAdapter(api_key="test-key")


@pytest.fixture
def fake():
    return _FakeSession()


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "wandb"

    def test_max(self, adapter):
        assert adapter.profile().max_chunk_bytes == 10485760


class TestWriteRead:
    @pytest.mark.asyncio
    async def test_roundtrip(self, adapter, fake):
        with patch("aiohttp.ClientSession", return_value=fake):
            ref = await adapter.write_chunk("mlchunk")
            assert ref.adapter_name == "wandb"
            assert "wandb://" in ref.location
            result = await adapter.read_chunk(ref)
            assert result == "mlchunk"

    @pytest.mark.asyncio
    async def test_multi(self, adapter, fake):
        with patch("aiohttp.ClientSession", return_value=fake):
            refs = [await adapter.write_chunk(f"w{i}") for i in range(3)]
            for i, ref in enumerate(refs):
                assert await adapter.read_chunk(ref) == f"w{i}"


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete(self, adapter, fake):
        with patch("aiohttp.ClientSession", return_value=fake):
            ref = await adapter.write_chunk("bye")
            assert await adapter.delete_chunk(ref) is True


class TestList:
    @pytest.mark.asyncio
    async def test_list(self, adapter):
        adapter._cache["wandb://e/p/art_1"] = "d"
        chunks = await adapter.list_chunks("e/p")
        assert len(chunks) == 1


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
