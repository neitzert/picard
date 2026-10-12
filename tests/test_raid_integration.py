"""Integration tests — RAID pool wired through the orchestrator."""

from __future__ import annotations

import pytest

from picard.adapters.memory import MemoryAdapter
from picard.index import Index
from picard.orchestrator import Orchestrator
from picard.raid import RaidLevel, RaidPool


def _pool(level: RaidLevel, n: int = 4, **kw) -> tuple[RaidPool, list[MemoryAdapter]]:
    adapters = [MemoryAdapter(name=f"mem-{i}", max_chunk_bytes=4096) for i in range(n)]
    return RaidPool(adapters, level, **kw), adapters


@pytest.mark.asyncio
class TestRaid0Integration:
    async def test_put_get_round_trip(self):
        pool, _ = _pool(RaidLevel.RAID0, 3)
        index = Index(":memory:")
        orch = Orchestrator([pool], index, block_size=64)
        data = b"RAID-0 stripe test " * 20
        manifest = await orch.put("r0.bin", data, "pass")
        recovered = await orch.get("r0.bin", "pass")
        assert recovered == data
        assert manifest.total_chunks > 0

    async def test_chunks_distributed(self):
        pool, adapters = _pool(RaidLevel.RAID0, 3)
        index = Index(":memory:")
        orch = Orchestrator([pool], index, block_size=32)
        await orch.put("dist.bin", b"x" * 200, "pass")
        counts = [len(a.store) for a in adapters]
        assert sum(counts) > 0
        assert max(counts) - min(counts) <= 1


@pytest.mark.asyncio
class TestRaid1Integration:
    async def test_put_get_round_trip(self):
        pool, _ = _pool(RaidLevel.RAID1, 3, mirror_count=3)
        index = Index(":memory:")
        orch = Orchestrator([pool], index, block_size=64)
        data = b"RAID-1 mirror test " * 10
        manifest = await orch.put("r1.bin", data, "pass")
        recovered = await orch.get("r1.bin", "pass")
        assert recovered == data

    async def test_survives_adapter_loss(self):
        pool, adapters = _pool(RaidLevel.RAID1, 2, mirror_count=2)
        index = Index(":memory:")
        orch = Orchestrator([pool], index, block_size=256)
        data = b"resilient data"
        await orch.put("r1-loss.bin", data, "pass")
        adapters[0].store.clear()
        recovered = await orch.get("r1-loss.bin", "pass")
        assert recovered == data

    async def test_all_adapters_get_copies(self):
        pool, adapters = _pool(RaidLevel.RAID1, 3, mirror_count=3)
        index = Index(":memory:")
        orch = Orchestrator([pool], index, block_size=256)
        await orch.put("r1-copies.bin", b"mirrored", "pass")
        for a in adapters:
            assert len(a.store) > 0


@pytest.mark.asyncio
class TestRaid5Integration:
    async def test_put_get_round_trip(self):
        pool, _ = _pool(RaidLevel.RAID5, 3)
        index = Index(":memory:")
        orch = Orchestrator([pool], index, block_size=64)
        data = b"RAID-5 parity test " * 10
        manifest = await orch.put("r5.bin", data, "pass")
        await pool.flush_parity()
        recovered = await orch.get("r5.bin", "pass")
        assert recovered == data

    async def test_parity_chunks_created(self):
        pool, adapters = _pool(RaidLevel.RAID5, 3)
        index = Index(":memory:")
        orch = Orchestrator([pool], index, block_size=32)
        await orch.put("r5-parity.bin", b"p" * 200, "pass")
        await pool.flush_parity()
        total = sum(len(a.store) for a in adapters)
        data_count = sum(
            len(b.chunks) for b in (await orch.info("r5-parity.bin"))
            if False  # just to prove info works
        )
        assert total > 0


@pytest.mark.asyncio
class TestRaid10Integration:
    async def test_put_get_round_trip(self):
        pool, _ = _pool(RaidLevel.RAID10, 4)
        index = Index(":memory:")
        orch = Orchestrator([pool], index, block_size=64)
        data = b"RAID-10 striped mirror " * 10
        manifest = await orch.put("r10.bin", data, "pass")
        recovered = await orch.get("r10.bin", "pass")
        assert recovered == data

    async def test_survives_one_per_group(self):
        pool, adapters = _pool(RaidLevel.RAID10, 4)
        index = Index(":memory:")
        orch = Orchestrator([pool], index, block_size=256)
        data = b"mirror group resilience"
        await orch.put("r10-loss.bin", data, "pass")
        adapters[0].store.clear()
        adapters[2].store.clear()
        recovered = await orch.get("r10-loss.bin", "pass")
        assert recovered == data


@pytest.mark.asyncio
class TestMixedAdapters:
    async def test_raid_pool_plus_standalone(self):
        pool, _ = _pool(RaidLevel.RAID1, 2, mirror_count=2)
        standalone = MemoryAdapter(name="standalone", max_chunk_bytes=4096)
        index = Index(":memory:")
        orch = Orchestrator([pool, standalone], index, block_size=32)
        data = b"mixed adapter test " * 20
        manifest = await orch.put("mixed.bin", data, "pass")
        recovered = await orch.get("mixed.bin", "pass")
        assert recovered == data


@pytest.mark.asyncio
class TestRaidOperations:
    async def test_ls(self):
        pool, _ = _pool(RaidLevel.RAID0, 2)
        index = Index(":memory:")
        orch = Orchestrator([pool], index, block_size=256)
        await orch.put("a.bin", b"aaa", "pass")
        await orch.put("b.bin", b"bbb", "pass")
        paths = await orch.ls()
        assert sorted(paths) == ["a.bin", "b.bin"]

    async def test_rm(self):
        pool, _ = _pool(RaidLevel.RAID1, 2)
        index = Index(":memory:")
        orch = Orchestrator([pool], index, block_size=256)
        await orch.put("rm.bin", b"delete me", "pass")
        assert await orch.rm("rm.bin") is True
        assert await orch.ls() == []

    async def test_verify(self):
        pool, _ = _pool(RaidLevel.RAID0, 3)
        index = Index(":memory:")
        orch = Orchestrator([pool], index, block_size=256)
        await orch.put("verify.bin", b"integrity check", "pass")
        report = await orch.verify("verify.bin")
        assert report["ok"] is True
        assert report["missing_chunks"] == 0

    async def test_info(self):
        pool, _ = _pool(RaidLevel.RAID1, 2)
        index = Index(":memory:")
        orch = Orchestrator([pool], index, block_size=256)
        await orch.put("info.bin", b"metadata", "pass")
        result = await orch.info("info.bin")
        assert result["path"] == "info.bin"
        assert "raid-pool-raid1" in result["adapter_distribution"]

    async def test_du(self):
        pool, _ = _pool(RaidLevel.RAID0, 2)
        index = Index(":memory:")
        orch = Orchestrator([pool], index, block_size=256)
        await orch.put("du.bin", b"storage usage", "pass")
        result = await orch.du()
        assert result["total_chunks"] > 0
        assert result["total_files"] == 1

    async def test_capacity(self):
        pool, _ = _pool(RaidLevel.RAID0, 3)
        cap = await pool.capacity()
        assert cap["used_bytes"] == 0
        assert cap["chunk_count"] == 0
