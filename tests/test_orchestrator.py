"""End-to-end tests: orchestrator over the memory adapter."""

import pytest

from picard.adapters.memory import MemoryAdapter
from picard.index import Index
from picard.keys import KeyRing, load_master_key
from picard.orchestrator import Orchestrator


@pytest.fixture
def orch():
    adapter = MemoryAdapter(max_chunk_bytes=256)
    index = Index()  # in-memory SQLite
    return Orchestrator([adapter], index, block_size=128)


@pytest.fixture
def orch_with_keys():
    adapter = MemoryAdapter(max_chunk_bytes=512)
    index = Index()
    mk = load_master_key(passphrase="test-passphrase")
    ring = KeyRing(mk)
    return Orchestrator([adapter], index, block_size=128, key_ring=ring)


@pytest.mark.asyncio
class TestPutGet:
    async def test_small_file(self, orch):
        data = b"hello picard"
        await orch.put("/test.txt", data, "pw")
        result = await orch.get("/test.txt", "pw")
        assert result == data

    async def test_multi_block_file(self, orch):
        # block_size=128, so 500 bytes => 4 blocks
        data = b"x" * 500
        manifest = await orch.put("/big.bin", data, "pw")
        assert manifest.total_blocks == 4
        result = await orch.get("/big.bin", "pw")
        assert result == data

    async def test_empty_file(self, orch):
        await orch.put("/empty", b"", "pw")
        result = await orch.get("/empty", "pw")
        assert result == b""

    async def test_wrong_passphrase_raises(self, orch):
        await orch.put("/secret", b"classified", "right")
        with pytest.raises(Exception):
            await orch.get("/secret", "wrong")


@pytest.mark.asyncio
class TestLsRm:
    async def test_ls_empty(self, orch):
        assert await orch.ls() == []

    async def test_ls_after_put(self, orch):
        await orch.put("/a.txt", b"a", "pw")
        await orch.put("/b.txt", b"b", "pw")
        paths = await orch.ls()
        assert "/a.txt" in paths
        assert "/b.txt" in paths

    async def test_rm(self, orch):
        await orch.put("/del.txt", b"bye", "pw")
        assert await orch.rm("/del.txt")
        assert "/del.txt" not in await orch.ls()

    async def test_rm_nonexistent(self, orch):
        assert not await orch.rm("/nope")

    async def test_get_after_rm_raises(self, orch):
        await orch.put("/gone.txt", b"data", "pw")
        await orch.rm("/gone.txt")
        with pytest.raises(FileNotFoundError):
            await orch.get("/gone.txt", "pw")


@pytest.mark.asyncio
class TestPutGetWithKeyRing:
    async def test_round_trip_with_envelope(self, orch_with_keys):
        data = b"sealed secret"
        await orch_with_keys.put("/sealed.txt", data, "pw")
        result = await orch_with_keys.get("/sealed.txt", "pw")
        assert result == data

    async def test_multi_block_with_envelope(self, orch_with_keys):
        data = b"x" * 500
        manifest = await orch_with_keys.put("/big.bin", data, "pw")
        assert manifest.total_blocks == 4
        result = await orch_with_keys.get("/big.bin", "pw")
        assert result == data
