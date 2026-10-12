"""Tests for the Stellar adapter -- mocked, no network calls."""

from __future__ import annotations

import base64
import hashlib
from unittest.mock import AsyncMock, patch

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
        assert p.max_chunk_bytes == 6144
        assert p.persistent is True
        assert p.ordered is False
        assert p.rate_limit_per_min == 30

    async def test_write_chunk(self):
        pytest.importorskip("stellar_sdk")
        a = _make_adapter()
        enc = _sample_encoded()
        with patch("asyncio.to_thread", new=AsyncMock(return_value="txhash123")):
            ref = await a.write_chunk(enc)
        assert ref.adapter_name == "stellar"
        assert ref.location == "xlm://txhash123"

    async def test_read_chunk_with_manage_data(self):
        a = _make_adapter()
        enc = _sample_encoded()
        chunk_id = hashlib.sha256(enc.encode()).hexdigest()[:16]
        a._get = AsyncMock(return_value={
            "_embedded": {
                "records": [{
                    "type": "manage_data",
                    "name": f"pcd-{chunk_id}-0000",
                    "value": base64.b64encode(enc.encode()).decode(),
                }]
            }
        })
        ref = StoredChunkRef(adapter_name="stellar", location="xlm://txhash123")
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_read_chunk_not_found(self):
        a = _make_adapter()
        a._get = AsyncMock(return_value={"_embedded": {"records": []}})
        ref = StoredChunkRef(adapter_name="stellar", location="xlm://missing")
        with pytest.raises(ValueError, match="no picard data in tx"):
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
