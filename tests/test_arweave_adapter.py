"""Tests for the Arweave adapter -- mocked, no real permaweb."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.arweave_adapter import ArweaveAdapter
from picard.chunker import Chunk


def _sample_encoded(manifest_id: str = "m1", block: int = 0, idx: int = 0) -> str:
    return Chunk(
        version=1, manifest_id=manifest_id, block_index=block,
        chunk_index=idx, total_chunks=1, payload=b"test-payload",
    ).encode()


def _fake_jwk() -> dict:
    n_bytes = b"\x00" * 256
    return {
        "n": base64.urlsafe_b64encode(n_bytes).rstrip(b"=").decode(),
        "e": base64.urlsafe_b64encode(b"\x01\x00\x01").rstrip(b"=").decode(),
        "d": base64.urlsafe_b64encode(b"\x00" * 256).rstrip(b"=").decode(),
        "p": base64.urlsafe_b64encode(b"\x00" * 128).rstrip(b"=").decode(),
        "q": base64.urlsafe_b64encode(b"\x00" * 128).rstrip(b"=").decode(),
    }


def _make_adapter(**kw) -> ArweaveAdapter:
    defaults = dict(wallet_jwk=_fake_jwk(), gateway_url="https://arweave.example.com")
    defaults.update(kw)
    return ArweaveAdapter(**defaults)


def _mock_session(method="get", status=200, json_data=None, read_data=None, text_data=None):
    resp = AsyncMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    if json_data is not None:
        resp.json = AsyncMock(return_value=json_data)
    if read_data is not None:
        resp.read = AsyncMock(return_value=read_data)
    if text_data is not None:
        resp.text = AsyncMock(return_value=text_data)
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    session = AsyncMock()
    setattr(session, method, MagicMock(return_value=cm))
    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    return session_cm, session


@pytest.mark.asyncio
class TestArweaveProfile:
    async def test_defaults(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "arweave"
        assert p.max_chunk_bytes == 12_000_000
        assert p.persistent is True
        assert p.rate_limit_per_min == 30


@pytest.mark.asyncio
class TestArweaveRead:
    @patch("aiohttp.ClientSession")
    async def test_read_chunk(self, MockSession):
        enc = _sample_encoded()
        session_cm, _ = _mock_session("get", read_data=enc.encode())
        MockSession.return_value = session_cm
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="arweave", location="ar://some_tx_id")
        result = await a.read_chunk(ref)
        assert result == enc

    @patch("aiohttp.ClientSession")
    async def test_read_not_found(self, MockSession):
        session_cm, _ = _mock_session("get", status=404)
        MockSession.return_value = session_cm
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="arweave", location="ar://missing_tx")
        with pytest.raises(KeyError):
            await a.read_chunk(ref)


@pytest.mark.asyncio
class TestArweaveDelete:
    async def test_delete_returns_false(self):
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="arweave", location="ar://some_tx")
        assert await a.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestArweaveHealth:
    @patch("aiohttp.ClientSession")
    async def test_health_ok(self, MockSession):
        session_cm, _ = _mock_session("get")
        MockSession.return_value = session_cm
        a = _make_adapter()
        assert await a.health_check() is True

    @patch("aiohttp.ClientSession")
    async def test_health_fail(self, MockSession):
        MockSession.side_effect = Exception("err")
        a = _make_adapter()
        assert await a.health_check() is False


@pytest.mark.asyncio
class TestArweaveCapacity:
    @patch("aiohttp.ClientSession")
    async def test_capacity(self, MockSession):
        session_cm, _ = _mock_session("get", text_data="1000000000")
        MockSession.return_value = session_cm
        a = _make_adapter()
        cap = await a.capacity()
        assert cap["free_bytes"] == 1000000000


@pytest.mark.asyncio
class TestArweaveAddress:
    async def test_get_address(self):
        a = _make_adapter()
        addr = a._get_address()
        assert isinstance(addr, str)
        assert len(addr) > 10
