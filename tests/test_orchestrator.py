"""End-to-end tests: orchestrator over the memory adapter."""

import hashlib

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


@pytest.mark.asyncio
class TestInfo:
    async def test_basic(self, orch):
        data = b"hello info"
        await orch.put("/info.txt", data, "pw")
        result = await orch.info("/info.txt")
        assert result["path"] == "/info.txt"
        assert result["original_size"] == len(data)
        assert result["total_blocks"] >= 1
        assert result["total_chunks"] >= 1
        assert result["all_stored"] is True
        assert result["plaintext_hash"] == hashlib.sha256(data).hexdigest()

    async def test_not_found(self, orch):
        with pytest.raises(FileNotFoundError):
            await orch.info("/nope")

    async def test_adapter_distribution(self, orch):
        await orch.put("/dist.txt", b"data", "pw")
        result = await orch.info("/dist.txt")
        assert "memory" in result["adapter_distribution"]


@pytest.mark.asyncio
class TestVerify:
    async def test_ok(self, orch):
        await orch.put("/v.txt", b"verify me", "pw")
        report = await orch.verify("/v.txt")
        assert report["ok"] is True
        assert report["missing_chunks"] == 0
        assert report["corrupt_blocks"] == 0

    async def test_missing_chunk(self, orch):
        await orch.put("/v2.txt", b"verify me", "pw")
        adapter = orch._adapters[0]
        adapter.store.clear()
        report = await orch.verify("/v2.txt")
        assert report["ok"] is False
        assert report["missing_chunks"] > 0

    async def test_not_found(self, orch):
        with pytest.raises(FileNotFoundError):
            await orch.verify("/nope")


@pytest.mark.asyncio
class TestCpMv:
    async def test_cp(self, orch):
        data = b"copy me"
        await orch.put("/orig.txt", data, "pw")
        await orch.cp("/orig.txt", "/copy.txt")
        result = await orch.get("/copy.txt", "pw")
        assert result == data
        orig = await orch.get("/orig.txt", "pw")
        assert orig == data

    async def test_mv(self, orch):
        data = b"move me"
        await orch.put("/old.txt", data, "pw")
        await orch.mv("/old.txt", "/new.txt")
        result = await orch.get("/new.txt", "pw")
        assert result == data

    async def test_cp_not_found(self, orch):
        with pytest.raises(FileNotFoundError):
            await orch.cp("/nope", "/dst")

    async def test_mv_not_found(self, orch):
        with pytest.raises(FileNotFoundError):
            await orch.mv("/nope", "/dst")


@pytest.mark.asyncio
class TestDu:
    async def test_empty(self, orch):
        result = await orch.du()
        assert result["total_files"] == 0
        assert result["total_chunks"] == 0

    async def test_with_files(self, orch):
        await orch.put("/a.txt", b"aaa", "pw")
        await orch.put("/b.txt", b"bbb", "pw")
        result = await orch.du()
        assert result["total_files"] == 2
        assert result["total_chunks"] >= 2
        assert result["total_bytes"] > 0
        assert "memory" in result["adapters"]


@pytest.mark.asyncio
class TestRoundRobin:
    async def test_chunks_distributed_across_adapters(self):
        adapters = [MemoryAdapter(name=f"a{i}") for i in range(4)]
        index = Index()
        orch = Orchestrator(adapters, index, block_size=1)
        manifest = await orch.put("rr.txt", b"ABCDEFGH", "pw")
        names = [cr.adapter_name for b in manifest.blocks for cr in b.chunks]
        assert set(names) == {"a0", "a1", "a2", "a3"}

    async def test_round_robin_cycles(self):
        adapters = [MemoryAdapter(name=f"n{i}") for i in range(3)]
        index = Index()
        orch = Orchestrator(adapters, index, block_size=1)
        manifest = await orch.put("cycle.txt", b"123456", "pw")
        names = [cr.adapter_name for b in manifest.blocks for cr in b.chunks]
        assert names == ["n0", "n1", "n2", "n0", "n1", "n2"]

    async def test_roundtrip_multi_adapter(self):
        adapters = [MemoryAdapter(name=f"m{i}") for i in range(5)]
        index = Index()
        orch = Orchestrator(adapters, index, block_size=4)
        data = b"The quick brown fox jumps"
        manifest = await orch.put("fox.txt", data, "pw")
        recovered = await orch.get("fox.txt", "pw")
        assert recovered == data
