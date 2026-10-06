"""Tests for the RADIUS adapter -- mocked, no real RADIUS server."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.radius_adapter import RadiusAdapter
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


def _make_adapter(**kwargs) -> RadiusAdapter:
    defaults = dict(
        server="127.0.0.1",
        secret=b"testing123",
    )
    defaults.update(kwargs)
    return RadiusAdapter(**defaults)


@pytest.mark.asyncio
class TestRadiusProfile:
    async def test_defaults(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "radius"
        assert p.max_chunk_bytes == 253
        assert p.persistent is False
        assert p.rate_limit_per_min == 60

    async def test_custom_max(self):
        a = _make_adapter(max_chunk_bytes=100)
        assert a.profile().max_chunk_bytes == 100


@pytest.mark.asyncio
class TestRadiusWrite:
    @patch("picard.adapters.radius_adapter.RadiusAdapter._make_client")
    async def test_write_chunk(self, mock_client_fn):
        client = MagicMock()
        req = MagicMock()
        client.CreateAuthPacket.return_value = req
        client.SendPacket.return_value = MagicMock()
        mock_client_fn.return_value = client

        a = _make_adapter()
        enc = _sample_encoded()
        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "radius"
        assert "radius://127.0.0.1/" in ref.location
        assert "m1" in ref.location
        assert a._store[ref.location.rsplit("/", 1)[-1]] == enc

    @patch("picard.adapters.radius_adapter.RadiusAdapter._make_client")
    async def test_write_opaque_data(self, mock_client_fn):
        client = MagicMock()
        req = MagicMock()
        client.CreateAuthPacket.return_value = req
        mock_client_fn.return_value = client

        a = _make_adapter()
        ref = await a.write_chunk("opaque-sealed-data")

        assert ref.adapter_name == "radius"
        assert a._store[ref.location.rsplit("/", 1)[-1]] == "opaque-sealed-data"

    @patch("picard.adapters.radius_adapter.RadiusAdapter._make_client")
    async def test_write_survives_send_failure(self, mock_client_fn):
        client = MagicMock()
        req = MagicMock()
        client.CreateAuthPacket.return_value = req
        client.SendPacket.side_effect = Exception("timeout")
        mock_client_fn.return_value = client

        a = _make_adapter()
        enc = _sample_encoded()
        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "radius"
        assert a._store[ref.location.rsplit("/", 1)[-1]] == enc


@pytest.mark.asyncio
class TestRadiusRead:
    @patch("picard.adapters.radius_adapter.RadiusAdapter._make_client")
    async def test_round_trip(self, mock_client_fn):
        client = MagicMock()
        req = MagicMock()
        client.CreateAuthPacket.return_value = req
        mock_client_fn.return_value = client

        a = _make_adapter()
        enc = _sample_encoded()
        ref = await a.write_chunk(enc)
        result = await a.read_chunk(ref)

        assert result == enc

    async def test_read_not_found(self):
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="radius", location="radius://127.0.0.1/missing")
        with pytest.raises(KeyError, match="chunk not found"):
            await a.read_chunk(ref)


@pytest.mark.asyncio
class TestRadiusDelete:
    @patch("picard.adapters.radius_adapter.RadiusAdapter._make_client")
    async def test_delete_success(self, mock_client_fn):
        client = MagicMock()
        req = MagicMock()
        client.CreateAuthPacket.return_value = req
        mock_client_fn.return_value = client

        a = _make_adapter()
        enc = _sample_encoded()
        ref = await a.write_chunk(enc)
        assert await a.delete_chunk(ref) is True
        assert len(a._store) == 0

    async def test_delete_not_found(self):
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="radius", location="radius://127.0.0.1/missing")
        assert await a.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestRadiusList:
    @patch("picard.adapters.radius_adapter.RadiusAdapter._make_client")
    async def test_list_chunks(self, mock_client_fn):
        client = MagicMock()
        req = MagicMock()
        client.CreateAuthPacket.return_value = req
        mock_client_fn.return_value = client

        a = _make_adapter()
        await a.write_chunk(_sample_encoded("m1", 0, 0))
        await a.write_chunk(_sample_encoded("m1", 0, 1))
        await a.write_chunk(_sample_encoded("m2", 0, 0))

        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert all(r.adapter_name == "radius" for r in refs)

    async def test_list_empty(self):
        a = _make_adapter()
        refs = await a.list_chunks("m1")
        assert refs == []


@pytest.mark.asyncio
class TestRadiusHealth:
    @patch("picard.adapters.radius_adapter.RadiusAdapter._make_client")
    async def test_health_ok(self, mock_client_fn):
        client = MagicMock()
        req = MagicMock()
        client.CreateAuthPacket.return_value = req
        client.SendPacket.return_value = MagicMock()
        mock_client_fn.return_value = client

        a = _make_adapter()
        assert await a.health_check() is True

    @patch("picard.adapters.radius_adapter.RadiusAdapter._make_client")
    async def test_health_fail(self, mock_client_fn):
        mock_client_fn.side_effect = Exception("connection refused")

        a = _make_adapter()
        assert await a.health_check() is False


@pytest.mark.asyncio
class TestRadiusCapacity:
    @patch("picard.adapters.radius_adapter.RadiusAdapter._make_client")
    async def test_capacity(self, mock_client_fn):
        client = MagicMock()
        req = MagicMock()
        client.CreateAuthPacket.return_value = req
        mock_client_fn.return_value = client

        a = _make_adapter()
        await a.write_chunk(_sample_encoded())
        cap = await a.capacity()
        assert cap["chunk_count"] == 1
        assert cap["used_bytes"] > 0
        assert cap["free_bytes"] is None
