"""Tests for the Solana adapter -- mocked, no network calls."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.solana_adapter import SolanaAdapter
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


def _make_adapter(**kwargs) -> SolanaAdapter:
    defaults = dict(
        rpc_url="https://api.mainnet-beta.solana.com",
        keypair_path=None,
    )
    defaults.update(kwargs)
    return SolanaAdapter(**defaults)


@pytest.mark.asyncio
class TestSolanaAdapter:
    async def test_profile_name(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "solana"

    async def test_profile_limits(self):
        a = _make_adapter()
        p = a.profile()
        assert p.max_chunk_bytes == 566
        assert p.persistent is True
        assert p.ordered is False
        assert p.rate_limit_per_min == 30

    async def test_write_chunk(self):
        a = _make_adapter()
        a._rpc = AsyncMock(side_effect=[
            {"result": {"value": {"blockhash": "11111111111111111111111111111111"}}},
            {"result": "txsig123"},
        ])
        enc = _sample_encoded()

        with patch("picard.adapters.solana_adapter._load_seed", return_value=b"\x00" * 32):
            ref = await a.write_chunk(enc)

        assert ref.adapter_name == "solana"
        assert ref.location == "sol://txsig123"
        assert a._rpc.call_args_list[1][0][0] == "sendTransaction"

    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        memo_b64 = base64.b64encode(enc.encode()).decode()

        a._rpc = AsyncMock(return_value={
            "result": {
                "transaction": {
                    "message": {
                        "instructions": [
                            {
                                "programId": SolanaAdapter.MEMO_PROGRAM_ID,
                                "data": memo_b64,
                            }
                        ]
                    }
                }
            }
        })

        ref = StoredChunkRef(adapter_name="solana", location="sol://txsig123")
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_read_chunk_not_found(self):
        a = _make_adapter()
        a._rpc = AsyncMock(return_value={"result": None})

        ref = StoredChunkRef(adapter_name="solana", location="sol://missing")
        with pytest.raises(ValueError, match="transaction not found"):
            await a.read_chunk(ref)

    async def test_read_chunk_no_memo(self):
        a = _make_adapter()
        a._rpc = AsyncMock(return_value={
            "result": {
                "transaction": {
                    "message": {
                        "instructions": [
                            {"programId": "SomeOtherProgram", "data": "abc"}
                        ]
                    }
                }
            }
        })

        ref = StoredChunkRef(adapter_name="solana", location="sol://txsig456")
        with pytest.raises(ValueError, match="no memo instruction"):
            await a.read_chunk(ref)

    async def test_delete_chunk_always_false(self):
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="solana", location="sol://txsig123")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        a._rpc = AsyncMock(return_value={"result": "ok"})
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        a._rpc = AsyncMock(return_value={"result": "behind"})
        assert await a.health_check() is False

    async def test_health_check_exception(self):
        a = _make_adapter()
        a._rpc = AsyncMock(side_effect=Exception("connection refused"))
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

    async def test_custom_program_id(self):
        a = _make_adapter(program_id="CustomMemoProgram123")
        assert a._program_id == "CustomMemoProgram123"
