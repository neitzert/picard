"""Tests for the Stellar adapter -- mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.stellar_adapter import StellarAdapter
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


def _make_adapter(**kwargs) -> StellarAdapter:
    defaults = dict(
        horizon_url="https://horizon.stellar.org",
        secret_key="STEST_SECRET_KEY",
    )
    defaults.update(kwargs)
    return StellarAdapter(**defaults)


@pytest.mark.asyncio
class TestStellarAdapter:
    async def test_profile_name(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "stellar"

    async def test_profile_limits(self):
        a = _make_adapter()
        p = a.profile()
        assert p.max_chunk_bytes == 64
        assert p.persistent is True
        assert p.ordered is False
        assert p.rate_limit_per_min == 30

    async def test_write_chunk(self):
        a = _make_adapter()
        a._post = AsyncMock(return_value={"hash": "txhash123"})
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "stellar"
        assert ref.location.startswith("xlm://")
        a._post.assert_awaited_once()
        call_args = a._post.call_args
        assert call_args[0][0] == "/transactions"

    async def test_read_chunk_with_memo(self):
        a = _make_adapter()
        enc = _sample_encoded()
        a._get = AsyncMock(return_value={"memo": enc})

        ref = StoredChunkRef(adapter_name="stellar", location="xlm://txhash123")
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_read_chunk_with_manage_data(self):
        a = _make_adapter()
        enc = _sample_encoded()

        async def _mock_get(path):
            if "/effects" in path:
                return {
                    "_embedded": {
                        "records": [
                            {"type": "manage_data", "value": enc},
                        ]
                    }
                }
            return {"memo": None}

        a._get = _mock_get

        ref = StoredChunkRef(adapter_name="stellar", location="xlm://txhash456")
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_read_chunk_not_found(self):
        a = _make_adapter()

        async def _mock_get(path):
            if "/effects" in path:
                return {"_embedded": {"records": []}}
            return {}

        a._get = _mock_get

        ref = StoredChunkRef(adapter_name="stellar", location="xlm://missing")
        with pytest.raises(ValueError, match="no chunk data"):
            await a.read_chunk(ref)

    async def test_delete_chunk_always_false(self):
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="stellar", location="xlm://txhash123")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        a._get = AsyncMock(return_value={"horizon_version": "2.0.0"})
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        a._get = AsyncMock(return_value={"error": "not available"})
        assert await a.health_check() is False

    async def test_health_check_exception(self):
        a = _make_adapter()
        a._get = AsyncMock(side_effect=Exception("connection refused"))
        assert await a.health_check() is False

    async def test_capacity(self):
        a = _make_adapter()
        cap = await a.capacity()
        assert cap == {
            "used_bytes": None,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": None,
        }

    async def test_custom_horizon_url(self):
        a = _make_adapter(horizon_url="https://horizon-testnet.stellar.org/")
        assert a._horizon_url == "https://horizon-testnet.stellar.org"

    async def test_custom_max_chunk(self):
        a = _make_adapter(max_chunk_bytes=128)
        p = a.profile()
        assert p.max_chunk_bytes == 128
