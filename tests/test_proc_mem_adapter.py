"""Tests for ProcMemAdapter.

All tests mock the Unix socket and SCM_RIGHTS operations so no companion
daemon or Linux memfd_create is required.
"""

from __future__ import annotations

import json
import mmap
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from picard.adapters.base import StoredChunkRef
from picard.adapters.proc_mem_adapter import (
    ProcMemAdapter,
    _SelfStore,
    _CompanionClient,
)


# ---------------------------------------------------------------------------
# _SelfStore unit tests (no mocking needed, cross-platform)
# ---------------------------------------------------------------------------

class TestSelfStore:
    def test_round_trip(self):
        store = _SelfStore()
        store.write("tok1", b"hello world")
        assert store.read("tok1") == b"hello world"

    def test_delete_returns_true(self):
        store = _SelfStore()
        store.write("tok1", b"data")
        assert store.delete("tok1") is True

    def test_delete_missing_returns_false(self):
        store = _SelfStore()
        assert store.delete("nonexistent") is False

    def test_read_missing_raises(self):
        store = _SelfStore()
        with pytest.raises(KeyError):
            store.read("missing")

    def test_list_tokens(self):
        store = _SelfStore()
        store.write("a", b"1")
        store.write("b", b"2")
        assert set(store.list_tokens()) == {"a", "b"}

    def test_close_clears(self):
        store = _SelfStore()
        store.write("tok", b"x")
        store.close()
        assert store.list_tokens() == []


# ---------------------------------------------------------------------------
# ProcMemAdapter self mode (cross-platform, no mocking needed)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestProcMemAdapterSelf:
    async def test_write_read_round_trip(self):
        adapter = ProcMemAdapter(mode="self")
        chunk = "PICARD:chunk:data"
        ref = await adapter.write_chunk(chunk)
        result = await adapter.read_chunk(ref)
        assert result == chunk

    async def test_delete_chunk(self):
        adapter = ProcMemAdapter(mode="self")
        ref = await adapter.write_chunk("hello")
        deleted = await adapter.delete_chunk(ref)
        assert deleted is True

    async def test_delete_twice_returns_false(self):
        adapter = ProcMemAdapter(mode="self")
        ref = await adapter.write_chunk("hello")
        await adapter.delete_chunk(ref)
        deleted_again = await adapter.delete_chunk(ref)
        assert deleted_again is False

    async def test_health_check_true(self):
        adapter = ProcMemAdapter(mode="self")
        assert await adapter.health_check() is True

    async def test_list_chunks_empty_manifest(self):
        adapter = ProcMemAdapter(mode="self")
        await adapter.write_chunk("data")
        refs = await adapter.list_chunks("other-manifest")
        assert refs == []

    async def test_profile(self):
        adapter = ProcMemAdapter(mode="self")
        p = adapter.profile()
        assert p.name == "proc-mem"
        assert p.persistent is False
        assert p.tags["tier"] == "4"
        assert p.tags["experimental"] == "true"

    async def test_chunk_too_large_raises(self):
        adapter = ProcMemAdapter(mode="self", max_chunk_bytes=10)
        with pytest.raises(ValueError, match="exceeds max"):
            await adapter.write_chunk("x" * 20)

    async def test_close_clears_store(self):
        adapter = ProcMemAdapter(mode="self")
        ref = await adapter.write_chunk("test")
        await adapter.close()
        assert adapter._store is None or not adapter._store.list_tokens()


# ---------------------------------------------------------------------------
# ProcMemAdapter memfd mode -- mocked _CompanionClient
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_client():
    client = MagicMock(spec=_CompanionClient)
    client.ping.return_value = True
    client.write.return_value = None
    client.read.return_value = b"PICARD:encoded:chunk"
    client.delete.return_value = True
    client.list_tokens.return_value = []
    return client


@pytest.mark.skipif(sys.platform != "linux", reason="memfd mode is Linux-only")
@pytest.mark.asyncio
class TestProcMemAdapterMemfd:
    async def test_write_chunk_calls_client_write(self, mock_client):
        adapter = ProcMemAdapter.__new__(ProcMemAdapter)
        adapter._mode = "memfd"
        adapter._max_chunk_bytes = 1_048_576
        adapter._registry = {}
        adapter._store = None
        adapter._client = mock_client

        ref = await adapter.write_chunk("PICARD:encoded:chunk")
        assert ref.adapter_name == "proc-mem"
        loc = json.loads(ref.location)
        assert "token" in loc
        assert loc["mode"] == "memfd"
        mock_client.write.assert_called_once()

    async def test_read_chunk_decodes_correctly(self, mock_client):
        adapter = ProcMemAdapter.__new__(ProcMemAdapter)
        adapter._mode = "memfd"
        adapter._max_chunk_bytes = 1_048_576
        adapter._registry = {}
        adapter._store = None
        adapter._client = mock_client

        ref = StoredChunkRef(
            adapter_name="proc-mem",
            location=json.dumps(
                {"token": "abc", "size": 20, "mode": "memfd"}
            ),
        )
        result = await adapter.read_chunk(ref)
        assert result == "PICARD:encoded:chunk"
        mock_client.read.assert_called_once_with("abc")

    async def test_delete_chunk_calls_client_delete(self, mock_client):
        adapter = ProcMemAdapter.__new__(ProcMemAdapter)
        adapter._mode = "memfd"
        adapter._max_chunk_bytes = 1_048_576
        adapter._registry = {"abc": ("", 20)}
        adapter._store = None
        adapter._client = mock_client

        ref = StoredChunkRef(
            adapter_name="proc-mem",
            location=json.dumps({"token": "abc", "size": 20, "mode": "memfd"}),
        )
        result = await adapter.delete_chunk(ref)
        assert result is True
        mock_client.delete.assert_called_once_with("abc")

    async def test_health_check_delegates_to_client_ping(self, mock_client):
        adapter = ProcMemAdapter.__new__(ProcMemAdapter)
        adapter._mode = "memfd"
        adapter._store = None
        adapter._client = mock_client

        assert await adapter.health_check() is True
        mock_client.ping.return_value = False
        assert await adapter.health_check() is False

    async def test_health_check_false_when_companion_unreachable(self):
        adapter = ProcMemAdapter.__new__(ProcMemAdapter)
        adapter._mode = "memfd"
        adapter._store = None
        bad_client = MagicMock(spec=_CompanionClient)
        bad_client.ping.side_effect = ConnectionRefusedError
        adapter._client = bad_client
        assert await adapter.health_check() is False


# ---------------------------------------------------------------------------
# Platform guard: memfd mode raises on non-Linux
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "linux", reason="tests non-Linux guard")
def test_memfd_mode_raises_on_non_linux():
    with pytest.raises(NotImplementedError, match="Linux"):
        ProcMemAdapter(mode="memfd")


# ---------------------------------------------------------------------------
# list_chunks filters by manifest_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_chunks_filters_by_manifest_id():
    adapter = ProcMemAdapter(mode="self")
    # Manually populate registry with different manifest IDs
    adapter._registry["tok1"] = ("manifest-A", 10)
    adapter._registry["tok2"] = ("manifest-B", 10)
    adapter._registry["tok3"] = ("manifest-A", 10)

    refs = await adapter.list_chunks("manifest-A")
    locations = [json.loads(r.location)["token"] for r in refs]
    assert set(locations) == {"tok1", "tok3"}
