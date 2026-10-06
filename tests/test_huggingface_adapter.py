"""Tests for Hugging Face Hub adapter — uses in-memory mock."""

from __future__ import annotations

import sys
import types
from unittest.mock import patch

import pytest

_aiohttp = types.ModuleType("aiohttp")
_aiohttp.ClientSession = type("ClientSession", (), {})
sys.modules.setdefault("aiohttp", _aiohttp)

from picard.adapters.huggingface_adapter import HuggingFaceAdapter


class _FakeResponse:
    def __init__(self, data=None, text_data=None, status=200):
        self._data = data or {}
        self._text = text_data
        self.status = status

    def raise_for_status(self):
        pass

    async def json(self):
        return self._data

    async def text(self):
        return self._text or ""


class _FakeSession:
    def __init__(self):
        self._files: dict[str, str] = {}

    async def put(self, url, **kwargs):
        data = kwargs.get("data", b"")
        self._files[url] = data.decode() if isinstance(data, bytes) else data
        return _FakeResponse()

    async def get(self, url, **kwargs):
        if "whoami" in url:
            return _FakeResponse({"username": "test"})
        for stored_url, content in self._files.items():
            if any(part in url for part in stored_url.split("/")):
                return _FakeResponse(text_data=content)
        return _FakeResponse(text_data="", status=404)

    async def delete(self, url, **kwargs):
        return _FakeResponse()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass


@pytest.fixture
def adapter():
    return HuggingFaceAdapter(token="hf_test123")


@pytest.fixture
def fake():
    return _FakeSession()


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "huggingface"

    def test_max(self, adapter):
        assert adapter.profile().max_chunk_bytes == 10485760


class TestWriteRead:
    @pytest.mark.asyncio
    async def test_roundtrip(self, adapter, fake):
        with patch("aiohttp.ClientSession", return_value=fake):
            ref = await adapter.write_chunk("mldata")
            assert ref.adapter_name == "huggingface"
            assert "hf://" in ref.location
            result = await adapter.read_chunk(ref)
            assert result == "mldata"

    @pytest.mark.asyncio
    async def test_multi(self, adapter, fake):
        with patch("aiohttp.ClientSession", return_value=fake):
            refs = [await adapter.write_chunk(f"d{i}") for i in range(3)]
            for i, ref in enumerate(refs):
                assert await adapter.read_chunk(ref) == f"d{i}"


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete(self, adapter, fake):
        with patch("aiohttp.ClientSession", return_value=fake):
            ref = await adapter.write_chunk("gone")
            assert await adapter.delete_chunk(ref) is True


class TestList:
    @pytest.mark.asyncio
    async def test_list(self, adapter):
        adapter._cache["hf://repo/file1"] = "d"
        chunks = await adapter.list_chunks("repo")
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

    @pytest.mark.asyncio
    async def test_with_data(self, adapter, fake):
        with patch("aiohttp.ClientSession", return_value=fake):
            await adapter.write_chunk("payload")
            cap = await adapter.capacity()
            assert cap["chunk_count"] == 1
