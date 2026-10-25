"""Tests for MempoolAdapter.

All Ethereum node interaction is mocked. No running node is required.
"""

from __future__ import annotations

import json
import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest
import pytest_asyncio


# ---------------------------------------------------------------------------
# Helpers to build a minimal web3 mock that satisfies the adapter's imports
# ---------------------------------------------------------------------------

def _make_web3_mock(txhash_hex: str = "0xdeadbeef", chain_id: int = 1337):
    """Return a (web3_module_mock, Web3_class_mock) pair."""
    web3_mod = ModuleType("web3")

    acct_mock = MagicMock()
    acct_mock.address = "0xAbCd" * 10  # 40 hex chars
    # Use a MagicMock for .key so .key.hex() is patchable
    key_mock = MagicMock()
    key_mock.hex = MagicMock(return_value="0x" + "a" * 64)
    key_mock.__bytes__ = lambda self: bytes.fromhex("a" * 64)
    acct_mock.key = key_mock

    account_cls = MagicMock()
    account_cls.create.return_value = acct_mock
    account_cls.from_key.return_value = acct_mock
    signed = MagicMock()
    signed.rawTransaction = b"\x00" * 32
    account_cls.sign_transaction.return_value = signed

    eth_mock = MagicMock()
    eth_mock.chain_id = chain_id
    eth_mock.get_transaction_count.return_value = 0
    eth_mock.send_raw_transaction.return_value = bytes.fromhex(
        txhash_hex.removeprefix("0x")
    )
    eth_mock.account = account_cls

    tx_record = {
        "input": b"hello-chunk",
        "nonce": 1000,
        "hash": txhash_hex,
    }
    eth_mock.get_transaction.return_value = tx_record

    w3_instance = MagicMock()
    w3_instance.eth = eth_mock

    Web3_cls = MagicMock(return_value=w3_instance)
    Web3_cls.HTTPProvider = MagicMock(return_value=MagicMock())

    # Web3() called with no args (for key gen) also returns something with .eth
    bare_w3 = MagicMock()
    bare_w3.eth = eth_mock
    Web3_cls.side_effect = lambda *a, **kw: bare_w3 if not a else w3_instance

    exc_mod = ModuleType("web3.exceptions")

    class TransactionNotFound(Exception):
        pass

    exc_mod.TransactionNotFound = TransactionNotFound

    web3_mod.Web3 = Web3_cls
    web3_mod.exceptions = exc_mod
    sys.modules["web3"] = web3_mod
    sys.modules["web3.exceptions"] = exc_mod

    return web3_mod, Web3_cls, w3_instance, eth_mock, acct_mock


# ---------------------------------------------------------------------------
# Fixture: patch web3 before importing the adapter
# ---------------------------------------------------------------------------

@pytest.fixture()
def w3_env():
    """Install mock web3 and yield the mock pieces."""
    web3_mod, Web3_cls, w3_instance, eth_mock, acct_mock = _make_web3_mock(
        txhash_hex="0xdeadbeefcafe"
    )
    # Force re-import with mock in place
    if "picard.adapters.mempool_adapter" in sys.modules:
        del sys.modules["picard.adapters.mempool_adapter"]

    import importlib
    import picard.adapters.mempool_adapter as mod
    importlib.reload(mod)

    yield mod, web3_mod, Web3_cls, w3_instance, eth_mock, acct_mock

    # Cleanup
    for key in list(sys.modules):
        if "mempool_adapter" in key:
            del sys.modules[key]
    sys.modules.pop("web3", None)
    sys.modules.pop("web3.exceptions", None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestMempoolAdapterProfile:
    def test_profile_fields(self, w3_env):
        mod, *_ = w3_env
        adapter = mod.MempoolAdapter(private_key="a" * 64)
        p = adapter.profile()
        assert p.name == "mempool"
        assert p.persistent is False
        assert p.max_chunk_bytes == 65536
        assert p.tags["tier"] == "4"
        assert p.tags["experimental"] == "true"


class TestMempoolAdapterWrite:
    @pytest.mark.asyncio
    async def test_write_chunk_returns_ref(self, w3_env):
        mod, _, _, w3_instance, eth_mock, _ = w3_env
        adapter = mod.MempoolAdapter(private_key="a" * 64)
        ref = await adapter.write_chunk("test-data")
        assert ref.adapter_name == "mempool"
        loc = json.loads(ref.location)
        assert "txhash" in loc
        assert "nonce" in loc
        assert loc["data_length"] == len("test-data".encode())

    @pytest.mark.asyncio
    async def test_slot_index_increments(self, w3_env):
        mod, _, _, w3_instance, eth_mock, _ = w3_env
        adapter = mod.MempoolAdapter(private_key="a" * 64, nonce_gap=500)
        eth_mock.get_transaction_count.return_value = 0

        ref1 = await adapter.write_chunk("chunk-a")
        ref2 = await adapter.write_chunk("chunk-b")

        loc1 = json.loads(ref1.location)
        loc2 = json.loads(ref2.location)
        # Second write nonce should be one higher than the first
        assert loc2["nonce"] == loc1["nonce"] + 1

    @pytest.mark.asyncio
    async def test_write_chunk_exceeds_max(self, w3_env):
        mod, *_ = w3_env
        adapter = mod.MempoolAdapter(private_key="a" * 64, max_chunk_bytes=10)
        with pytest.raises(ValueError, match="exceeds max"):
            await adapter.write_chunk("x" * 11)


class TestMempoolAdapterRead:
    @pytest.mark.asyncio
    async def test_read_chunk_decodes_data(self, w3_env):
        mod, _, _, w3_instance, eth_mock, _ = w3_env
        payload = "my-encrypted-chunk"
        eth_mock.get_transaction.return_value = {
            "input": payload.encode("utf-8"),
            "nonce": 1000,
        }
        adapter = mod.MempoolAdapter(private_key="a" * 64)
        ref = await adapter.write_chunk(payload)
        result = await adapter.read_chunk(ref)
        assert result == payload

    @pytest.mark.asyncio
    async def test_read_chunk_not_found(self, w3_env):
        mod, web3_mod, _Web3_cls, _w3_instance, eth_mock, _acct = w3_env
        eth_mock.get_transaction.side_effect = web3_mod.exceptions.TransactionNotFound(
            "not found"
        )
        adapter = mod.MempoolAdapter(private_key="a" * 64)
        ref_loc = json.dumps({"txhash": "0xdeadbeef", "nonce": 1000, "data_length": 5})
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="mempool", location=ref_loc)
        with pytest.raises(KeyError, match="not found in mempool"):
            await adapter.read_chunk(ref)


class TestMempoolAdapterDelete:
    @pytest.mark.asyncio
    async def test_delete_chunk_sends_rbf(self, w3_env):
        mod, _, _, w3_instance, eth_mock, _ = w3_env
        adapter = mod.MempoolAdapter(private_key="a" * 64)
        ref = await adapter.write_chunk("deletable-chunk")
        result = await adapter.delete_chunk(ref)
        assert result is True
        # RBF replacement was sent (send_raw_transaction called at least twice)
        assert eth_mock.send_raw_transaction.call_count >= 2

    @pytest.mark.asyncio
    async def test_delete_chunk_returns_false_on_error(self, w3_env):
        mod, _, _, w3_instance, eth_mock, _ = w3_env
        eth_mock.send_raw_transaction.side_effect = [
            bytes.fromhex("deadbeef"),   # first call (write) succeeds
            Exception("node rejected"),  # second call (delete RBF) fails
        ]
        adapter = mod.MempoolAdapter(private_key="a" * 64)
        ref = await adapter.write_chunk("chunk")
        result = await adapter.delete_chunk(ref)
        assert result is False


class TestMempoolAdapterHealth:
    @pytest.mark.asyncio
    async def test_health_check_true_when_reachable(self, w3_env):
        mod, _, _, w3_instance, eth_mock, _ = w3_env
        adapter = mod.MempoolAdapter(private_key="a" * 64)
        assert await adapter.health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_false_when_unreachable(self, w3_env):
        mod, _, _, w3_instance, eth_mock, _ = w3_env
        type(w3_instance.eth).chain_id = PropertyMock(
            side_effect=Exception("connection refused")
        )
        adapter = mod.MempoolAdapter(private_key="a" * 64)
        assert await adapter.health_check() is False


class TestMempoolAdapterImportGuard:
    def test_import_error_without_web3(self):
        """ImportError with install hint when web3 is absent."""
        saved = sys.modules.pop("web3", None)
        saved_exc = sys.modules.pop("web3.exceptions", None)
        saved_mod = sys.modules.pop("picard.adapters.mempool_adapter", None)

        try:
            import importlib
            import picard.adapters.mempool_adapter as mod
            importlib.reload(mod)
            with pytest.raises(ImportError, match="pip install web3"):
                mod.MempoolAdapter()
        finally:
            if saved is not None:
                sys.modules["web3"] = saved
            if saved_exc is not None:
                sys.modules["web3.exceptions"] = saved_exc
            if saved_mod is not None:
                sys.modules["picard.adapters.mempool_adapter"] = saved_mod
