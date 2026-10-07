"""Tests for the Cardano adapter -- mocked, no real Blockfrost calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.cardano_adapter import CardanoAdapter
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


def _make_adapter(**kwargs) -> CardanoAdapter:
    defaults = dict(
        api_url="https://test.blockfrost.io/api/v0",
        project_id="test-project",
    )
    defaults.update(kwargs)
    return CardanoAdapter(**defaults)


def _mock_response(json_data=None, status=200):
    resp = AsyncMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    if json_data is not None:
        resp.json = AsyncMock(return_value=json_data)
    return resp


def _patch_session(adapter, get_resp=None, post_resp=None):
    """Replace the adapter's session with a mock."""
    session = AsyncMock()

    if get_resp is not None:
        get_ctx = AsyncMock()
        get_ctx.__aenter__ = AsyncMock(return_value=get_resp)
        get_ctx.__aexit__ = AsyncMock(return_value=False)
        session.get = MagicMock(return_value=get_ctx)

    if post_resp is not None:
        post_ctx = AsyncMock()
        post_ctx.__aenter__ = AsyncMock(return_value=post_resp)
        post_ctx.__aexit__ = AsyncMock(return_value=False)
        session.post = MagicMock(return_value=post_ctx)

    adapter._session = session
    return session


@pytest.mark.asyncio
class TestCardanoAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "cardano"
        assert p.max_chunk_bytes == 16_384
        assert p.persistent is True
        assert p.ordered is False
        assert p.rate_limit_per_min == 10

    async def test_write_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()

        post_resp = _mock_response(json_data={"hash": "abc123"})
        _patch_session(a, post_resp=post_resp)

        ref = await a.write_chunk(enc)
        assert ref.adapter_name == "cardano"
        assert ref.location.startswith("ada://")

    async def test_read_chunk_string_metadata(self):
        a = _make_adapter()
        enc = _sample_encoded()

        get_resp = _mock_response(json_data=[
            {"label": "8888", "json_metadata": enc}
        ])
        _patch_session(a, get_resp=get_resp)

        ref = StoredChunkRef(adapter_name="cardano", location="ada://abc123")
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_read_chunk_dict_metadata(self):
        a = _make_adapter()
        enc = _sample_encoded()

        get_resp = _mock_response(json_data=[
            {"label": "8888", "json_metadata": {"chunk": enc}}
        ])
        _patch_session(a, get_resp=get_resp)

        ref = StoredChunkRef(adapter_name="cardano", location="ada://abc123")
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_read_chunk_missing_label_raises(self):
        a = _make_adapter()

        get_resp = _mock_response(json_data=[
            {"label": "9999", "json_metadata": "other"}
        ])
        _patch_session(a, get_resp=get_resp)

        ref = StoredChunkRef(adapter_name="cardano", location="ada://abc123")
        with pytest.raises(ValueError, match="no metadata label 8888"):
            await a.read_chunk(ref)

    async def test_delete_chunk_always_false(self):
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="cardano", location="ada://abc123")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks_always_empty(self):
        a = _make_adapter()
        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()

        get_resp = _mock_response(json_data={"is_healthy": True})
        _patch_session(a, get_resp=get_resp)

        assert await a.health_check() is True

    async def test_health_check_unhealthy(self):
        a = _make_adapter()

        get_resp = _mock_response(json_data={"is_healthy": False})
        _patch_session(a, get_resp=get_resp)

        assert await a.health_check() is False

    async def test_health_check_exception(self):
        a = _make_adapter()
        session = AsyncMock()
        get_ctx = AsyncMock()
        get_ctx.__aenter__ = AsyncMock(side_effect=Exception("timeout"))
        session.get = MagicMock(return_value=get_ctx)
        a._session = session

        assert await a.health_check() is False

    async def test_capacity(self):
        a = _make_adapter()
        cap = await a.capacity()
        assert cap["used_bytes"] is None
        assert cap["free_bytes"] is None
        assert cap["total_bytes"] is None
        assert cap["chunk_count"] is None

    async def test_metadata_label_constant(self):
        assert CardanoAdapter.METADATA_LABEL == 8888
