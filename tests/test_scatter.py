"""Tests for scatter profiles."""

from __future__ import annotations

import pytest

from picard.adapters.base import AdapterProfile
from picard.adapters.memory import MemoryAdapter
from picard.config import ScatterProfile
from picard.raid import RaidLevel, RaidPool
from picard.scatter import (
    BUILTIN_PROFILES,
    build_from_profile,
    list_profiles,
    resolve_profile,
    select_adapters,
)


class _TaggedAdapter(MemoryAdapter):
    """MemoryAdapter with custom tags for testing."""

    def __init__(self, name: str, tags: dict[str, str] | None = None):
        super().__init__(name=name, max_chunk_bytes=4096)
        self._tags = tags or {}

    def profile(self) -> AdapterProfile:
        p = super().profile()
        return AdapterProfile(
            name=p.name,
            max_chunk_bytes=p.max_chunk_bytes,
            persistent=p.persistent,
            ordered=p.ordered,
            tags=self._tags,
        )


class TestSelectAdapters:
    def test_no_filters_returns_all(self):
        adapters = [MemoryAdapter(name=f"a-{i}") for i in range(3)]
        result = select_adapters(adapters)
        assert len(result) == 3

    def test_filter_by_name(self):
        adapters = [MemoryAdapter(name=f"a-{i}") for i in range(3)]
        result = select_adapters(adapters, names=["a-1"])
        assert len(result) == 1
        assert result[0].profile().name == "a-1"

    def test_filter_by_tag(self):
        adapters = [
            _TaggedAdapter("fast-1", {"speed": "fast"}),
            _TaggedAdapter("slow-1", {"speed": "slow"}),
            _TaggedAdapter("fast-2", {"speed": "fast"}),
        ]
        result = select_adapters(adapters, tags=["fast"])
        assert len(result) == 2

    def test_name_or_tag(self):
        adapters = [
            _TaggedAdapter("cloud-a", {"type": "cloud"}),
            MemoryAdapter(name="local-b"),
            _TaggedAdapter("cloud-c", {"type": "cloud"}),
        ]
        result = select_adapters(adapters, names=["local-b"], tags=["cloud"])
        assert len(result) == 3

    def test_no_matches(self):
        adapters = [MemoryAdapter(name="a")]
        result = select_adapters(adapters, names=["nonexistent"])
        assert len(result) == 0


class TestBuildFromProfile:
    def test_raid0_profile(self):
        adapters = [MemoryAdapter(name=f"a-{i}") for i in range(3)]
        profile = ScatterProfile(
            name="test",
            raid_level="raid0",
            min_adapters=2,
        )
        result = build_from_profile(profile, adapters)
        assert len(result) == 1
        assert isinstance(result[0], RaidPool)
        assert result[0].profile().name == "raid-pool-raid0"

    def test_raid1_with_mirror_count(self):
        adapters = [MemoryAdapter(name=f"a-{i}") for i in range(3)]
        profile = ScatterProfile(
            name="test",
            raid_level="raid1",
            mirror_count=3,
            min_adapters=2,
        )
        result = build_from_profile(profile, adapters)
        assert len(result) == 1
        assert isinstance(result[0], RaidPool)

    def test_raid5_needs_3(self):
        adapters = [MemoryAdapter(name=f"a-{i}") for i in range(3)]
        profile = ScatterProfile(
            name="test",
            raid_level="raid5",
            min_adapters=3,
        )
        result = build_from_profile(profile, adapters)
        assert len(result) == 1

    def test_raid10_trims_odd(self):
        adapters = [MemoryAdapter(name=f"a-{i}") for i in range(5)]
        profile = ScatterProfile(
            name="test",
            raid_level="raid10",
            min_adapters=4,
        )
        result = build_from_profile(profile, adapters)
        assert len(result) == 1
        assert isinstance(result[0], RaidPool)

    def test_not_enough_adapters_falls_back(self):
        adapters = [MemoryAdapter(name="solo")]
        profile = ScatterProfile(
            name="test",
            raid_level="raid5",
            min_adapters=3,
        )
        result = build_from_profile(profile, adapters)
        assert len(result) == 1
        assert not isinstance(result[0], RaidPool)

    def test_filter_by_tags(self):
        adapters = [
            _TaggedAdapter("fast-1", {"speed": "fast"}),
            _TaggedAdapter("slow-1", {"speed": "slow"}),
            _TaggedAdapter("fast-2", {"speed": "fast"}),
        ]
        profile = ScatterProfile(
            name="test",
            raid_level="raid0",
            adapter_tags=["fast"],
            min_adapters=2,
        )
        result = build_from_profile(profile, adapters)
        assert len(result) == 1
        assert isinstance(result[0], RaidPool)

    def test_filter_by_names(self):
        adapters = [MemoryAdapter(name=f"a-{i}") for i in range(5)]
        profile = ScatterProfile(
            name="test",
            raid_level="raid1",
            adapter_names=["a-0", "a-2", "a-4"],
            min_adapters=2,
        )
        result = build_from_profile(profile, adapters)
        assert len(result) == 1
        assert isinstance(result[0], RaidPool)


class TestResolveProfile:
    def test_builtin_stealth(self):
        p = resolve_profile("stealth")
        assert p.name == "stealth"
        assert p.raid_level == "raid1"

    def test_builtin_resilient(self):
        p = resolve_profile("resilient")
        assert p.raid_level == "raid5"

    def test_builtin_speed(self):
        p = resolve_profile("speed")
        assert p.raid_level == "raid0"

    def test_builtin_paranoid(self):
        p = resolve_profile("paranoid")
        assert p.raid_level == "raid10"

    def test_custom_overrides_builtin(self):
        custom = ScatterProfile(name="stealth", raid_level="raid5", description="custom stealth")
        p = resolve_profile("stealth", [custom])
        assert p.raid_level == "raid5"

    def test_unknown_raises(self):
        with pytest.raises(KeyError, match="unknown scatter profile"):
            resolve_profile("nonexistent")


class TestListProfiles:
    def test_builtins_present(self):
        profiles = list_profiles()
        names = {p.name for p in profiles}
        assert "stealth" in names
        assert "resilient" in names
        assert "speed" in names
        assert "paranoid" in names

    def test_custom_added(self):
        custom = ScatterProfile(name="custom-1", description="test")
        profiles = list_profiles([custom])
        names = {p.name for p in profiles}
        assert "custom-1" in names

    def test_custom_overrides(self):
        custom = ScatterProfile(name="stealth", raid_level="raid0", description="fast stealth")
        profiles = list_profiles([custom])
        stealth = next(p for p in profiles if p.name == "stealth")
        assert stealth.raid_level == "raid0"


@pytest.mark.asyncio
class TestScatterIntegration:
    async def test_stealth_profile_round_trip(self):
        from picard.index import Index
        from picard.orchestrator import Orchestrator

        adapters = [_TaggedAdapter(f"steg-{i}", {"type": "steganographic"}) for i in range(3)]
        profile = resolve_profile("stealth")
        wrapped = build_from_profile(profile, adapters)

        index = Index(":memory:")
        orch = Orchestrator(wrapped, index, block_size=64)
        data = b"stealth scatter test " * 10
        await orch.put("stealth.bin", data, "pass")
        recovered = await orch.get("stealth.bin", "pass")
        assert recovered == data

    async def test_speed_profile_round_trip(self):
        from picard.index import Index
        from picard.orchestrator import Orchestrator

        adapters = [_TaggedAdapter(f"fast-{i}", {"perf": "fast"}) for i in range(3)]
        profile = resolve_profile("speed")
        wrapped = build_from_profile(profile, adapters)

        index = Index(":memory:")
        orch = Orchestrator(wrapped, index, block_size=64)
        data = b"speed scatter test " * 10
        await orch.put("speed.bin", data, "pass")
        recovered = await orch.get("speed.bin", "pass")
        assert recovered == data
