"""Tests for the RAID controller -- uses real MemoryAdapters."""

from __future__ import annotations

import pytest

from picard.adapters.memory import MemoryAdapter
from picard.chunker import Chunk
from picard.raid import RaidLevel, RaidPool, RaidRef, _xor_bytes


def _sample_encoded(manifest_id: str = "m1", block: int = 0, idx: int = 0) -> str:
    return Chunk(
        version=1,
        manifest_id=manifest_id,
        block_index=block,
        chunk_index=idx,
        total_chunks=1,
        payload=b"test-payload-data",
    ).encode()


def _adapters(n: int) -> list[MemoryAdapter]:
    return [MemoryAdapter(name=f"mem-{i}", max_chunk_bytes=4096) for i in range(n)]


class TestXorBytes:
    def test_same_length(self):
        assert _xor_bytes(b"\x0f\xf0", b"\xf0\x0f") == b"\xff\xff"

    def test_different_lengths(self):
        result = _xor_bytes(b"\xff", b"\xff\x00")
        assert len(result) == 2
        assert result[0] == 0x00
        assert result[1] == 0x00

    def test_empty(self):
        assert _xor_bytes(b"", b"") == b""

    def test_identity(self):
        data = b"hello"
        assert _xor_bytes(data, b"\x00" * len(data)) == data


class TestRaidRef:
    def test_pack_unpack_round_trip(self):
        from picard.adapters.base import StoredChunkRef
        refs = (
            StoredChunkRef(adapter_name="mem-0", location="loc-a"),
            StoredChunkRef(adapter_name="mem-1", location="loc-b"),
        )
        rr = RaidRef(refs=refs)
        packed = rr.pack()
        unpacked = RaidRef.unpack(packed)
        assert len(unpacked.refs) == 2
        assert unpacked.refs[0].adapter_name == "mem-0"
        assert unpacked.refs[1].location == "loc-b"

    def test_pack_unpack_with_parity(self):
        from picard.adapters.base import StoredChunkRef
        rr = RaidRef(
            refs=(StoredChunkRef(adapter_name="mem-0", location="d"),),
            parity_refs=(StoredChunkRef(adapter_name="mem-2", location="p"),),
            stripe_id=3,
        )
        packed = rr.pack()
        unpacked = RaidRef.unpack(packed)
        assert len(unpacked.parity_refs) == 1
        assert unpacked.stripe_id == 3


class TestRaidLevel:
    def test_values(self):
        assert RaidLevel.RAID0.value == "raid0"
        assert RaidLevel.RAID1.value == "raid1"
        assert RaidLevel.RAID5.value == "raid5"
        assert RaidLevel.RAID10.value == "raid10"


@pytest.mark.asyncio
class TestRaid0:
    async def test_profile(self):
        pool = RaidPool(_adapters(3), RaidLevel.RAID0)
        p = pool.profile()
        assert p.name == "raid-pool-raid0"
        assert p.ordered is False

    async def test_write_stripes_across_adapters(self):
        adapters = _adapters(3)
        pool = RaidPool(adapters, RaidLevel.RAID0)
        for i in range(3):
            await pool.write_chunk(_sample_encoded(idx=i))
        assert len(adapters[0].store) == 1
        assert len(adapters[1].store) == 1
        assert len(adapters[2].store) == 1

    async def test_round_robin_wraps(self):
        adapters = _adapters(2)
        pool = RaidPool(adapters, RaidLevel.RAID0)
        for i in range(4):
            await pool.write_chunk(_sample_encoded(idx=i))
        assert len(adapters[0].store) == 2
        assert len(adapters[1].store) == 2

    async def test_write_and_read(self):
        pool = RaidPool(_adapters(3), RaidLevel.RAID0)
        enc = _sample_encoded()
        ref = await pool.write_chunk(enc)
        result = await pool.read_chunk(ref)
        assert result == enc

    async def test_delete(self):
        pool = RaidPool(_adapters(2), RaidLevel.RAID0)
        ref = await pool.write_chunk(_sample_encoded())
        assert await pool.delete_chunk(ref) is True


@pytest.mark.asyncio
class TestRaid1:
    async def test_profile(self):
        pool = RaidPool(_adapters(3), RaidLevel.RAID1)
        assert pool.profile().name == "raid-pool-raid1"

    async def test_mirrors_to_all(self):
        adapters = _adapters(3)
        pool = RaidPool(adapters, RaidLevel.RAID1, mirror_count=3)
        await pool.write_chunk(_sample_encoded())
        assert len(adapters[0].store) == 1
        assert len(adapters[1].store) == 1
        assert len(adapters[2].store) == 1

    async def test_read(self):
        pool = RaidPool(_adapters(2), RaidLevel.RAID1)
        enc = _sample_encoded()
        ref = await pool.write_chunk(enc)
        result = await pool.read_chunk(ref)
        assert result == enc

    async def test_read_fallback(self):
        adapters = _adapters(2)
        pool = RaidPool(adapters, RaidLevel.RAID1)
        enc = _sample_encoded()
        ref = await pool.write_chunk(enc)
        adapters[0].store.clear()
        result = await pool.read_chunk(ref)
        assert result == enc

    async def test_delete_removes_all(self):
        adapters = _adapters(2)
        pool = RaidPool(adapters, RaidLevel.RAID1)
        ref = await pool.write_chunk(_sample_encoded())
        await pool.delete_chunk(ref)
        assert len(adapters[0].store) == 0
        assert len(adapters[1].store) == 0


@pytest.mark.asyncio
class TestRaid5:
    async def test_requires_3_adapters(self):
        with pytest.raises(ValueError, match="at least 3"):
            RaidPool(_adapters(2), RaidLevel.RAID5)

    async def test_write_and_read(self):
        pool = RaidPool(_adapters(3), RaidLevel.RAID5)
        enc = _sample_encoded()
        ref = await pool.write_chunk(enc)
        result = await pool.read_chunk(ref)
        assert result == enc

    async def test_parity_written_after_full_stripe(self):
        adapters = _adapters(3)
        pool = RaidPool(adapters, RaidLevel.RAID5)
        for i in range(2):
            await pool.write_chunk(_sample_encoded(idx=i))
        total_stored = sum(len(a.store) for a in adapters)
        assert total_stored == 3  # 2 data + 1 parity

    async def test_flush_partial_stripe(self):
        adapters = _adapters(3)
        pool = RaidPool(adapters, RaidLevel.RAID5)
        await pool.write_chunk(_sample_encoded())
        await pool.flush_parity()
        total_stored = sum(len(a.store) for a in adapters)
        assert total_stored == 2  # 1 data + 1 parity


@pytest.mark.asyncio
class TestRaid10:
    async def test_requires_4_adapters(self):
        with pytest.raises(ValueError, match="at least 4"):
            RaidPool(_adapters(3), RaidLevel.RAID10)

    async def test_requires_even(self):
        with pytest.raises(ValueError, match="even number"):
            RaidPool(_adapters(5), RaidLevel.RAID10)

    async def test_write_and_read(self):
        pool = RaidPool(_adapters(4), RaidLevel.RAID10)
        enc = _sample_encoded()
        ref = await pool.write_chunk(enc)
        result = await pool.read_chunk(ref)
        assert result == enc

    async def test_mirrors_within_group(self):
        adapters = _adapters(4)
        pool = RaidPool(adapters, RaidLevel.RAID10)
        await pool.write_chunk(_sample_encoded())
        group0_count = len(adapters[0].store) + len(adapters[1].store)
        group1_count = len(adapters[2].store) + len(adapters[3].store)
        assert group0_count == 2 or group1_count == 2

    async def test_mirror_fallback(self):
        adapters = _adapters(4)
        pool = RaidPool(adapters, RaidLevel.RAID10)
        enc = _sample_encoded()
        ref = await pool.write_chunk(enc)
        adapters[0].store.clear()
        result = await pool.read_chunk(ref)
        assert result == enc


@pytest.mark.asyncio
class TestRaidValidation:
    async def test_needs_at_least_2(self):
        with pytest.raises(ValueError, match="at least 2"):
            RaidPool([MemoryAdapter(name="solo")], RaidLevel.RAID0)

    async def test_health_raid0_all_required(self):
        pool = RaidPool(_adapters(2), RaidLevel.RAID0)
        assert await pool.health_check() is True

    async def test_health_raid1_one_enough(self):
        pool = RaidPool(_adapters(2), RaidLevel.RAID1)
        assert await pool.health_check() is True
