"""Tests for the environment variables adapter."""

import os

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.env_vars import EnvVarsAdapter, _PREFIX


@pytest.fixture
def adapter():
    a = EnvVarsAdapter()
    yield a
    # Clean up any env vars we set during tests
    to_remove = [k for k in os.environ if k.startswith(_PREFIX)]
    for k in to_remove:
        del os.environ[k]


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "env_vars"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is False

    def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 4096

    def test_ordered(self, adapter):
        assert adapter.profile().ordered is False


@pytest.mark.asyncio
class TestWrite:
    async def test_write_returns_ref(self, adapter):
        ref = await adapter.write_chunk("env data")
        assert ref.adapter_name == "env_vars"
        assert "env://" in ref.location

    async def test_write_sets_env_var(self, adapter):
        ref = await adapter.write_chunk("env value")
        var_name = ref.location.replace("env://", "")
        assert os.environ.get(var_name) == "env value"

    async def test_write_multiple(self, adapter):
        ref1 = await adapter.write_chunk("first")
        ref2 = await adapter.write_chunk("second")
        assert ref1.location != ref2.location


@pytest.mark.asyncio
class TestRead:
    async def test_read_from_env(self, adapter):
        ref = await adapter.write_chunk("hello env")
        assert await adapter.read_chunk(ref) == "hello env"

    async def test_read_not_found(self, adapter):
        ref = StoredChunkRef(
            adapter_name="env_vars",
            location="env://PICARD_CHUNK_missing",
        )
        with pytest.raises(KeyError):
            await adapter.read_chunk(ref)


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_existing(self, adapter):
        ref = await adapter.write_chunk("delete me")
        assert await adapter.delete_chunk(ref) is True
        var_name = ref.location.replace("env://", "")
        assert var_name not in os.environ

    async def test_delete_nonexistent(self, adapter):
        ref = StoredChunkRef(
            adapter_name="env_vars",
            location="env://PICARD_CHUNK_nope",
        )
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestListChunks:
    async def test_after_write(self, adapter):
        await adapter.write_chunk("a")
        await adapter.write_chunk("b")
        chunks = await adapter.list_chunks("any")
        assert len(chunks) >= 2


@pytest.mark.asyncio
class TestHealth:
    async def test_health_ok(self, adapter):
        assert await adapter.health_check() is True


@pytest.mark.asyncio
class TestCapacity:
    async def test_after_write(self, adapter):
        await adapter.write_chunk("data")
        cap = await adapter.capacity()
        assert cap["chunk_count"] >= 1
        assert cap["used_bytes"] is not None and cap["used_bytes"] > 0
