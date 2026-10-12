"""Scatter profiles — named presets for adapter selection and RAID level.

A scatter profile selects a subset of available adapters by name or tag,
wraps them in a RaidPool at the specified level, and returns the pool
ready for use by the orchestrator.

Built-in profiles:
  - quiet:     steganographic and unconventional-protocol adapters, RAID 1 mirroring
  - resilient: persistent/cloud adapters, RAID 5 parity
  - speed:     local/fast adapters, RAID 0 striping
  - paranoid:  all persistent adapters, RAID 10 striped mirrors
"""

from __future__ import annotations

from dataclasses import dataclass, field

from picard.adapters.base import Adapter
from picard.config import ScatterProfile
from picard.raid import RaidLevel, RaidPool

BUILTIN_PROFILES: dict[str, ScatterProfile] = {
    "quiet": ScatterProfile(
        name="quiet",
        raid_level="raid1",
        mirror_count=2,
        adapter_tags=["steganographic", "low-profile"],
        min_adapters=2,
        description="Steganographic and unconventional protocol adapters with RAID 1 mirroring",
    ),
    "resilient": ScatterProfile(
        name="resilient",
        raid_level="raid5",
        mirror_count=2,
        adapter_tags=["persistent", "cloud"],
        min_adapters=3,
        description="Maximum redundancy across reliable adapters",
    ),
    "speed": ScatterProfile(
        name="speed",
        raid_level="raid0",
        mirror_count=2,
        adapter_tags=["local", "fast"],
        min_adapters=2,
        description="Fast local scatter with no redundancy",
    ),
    "paranoid": ScatterProfile(
        name="paranoid",
        raid_level="raid10",
        mirror_count=2,
        adapter_tags=["persistent"],
        min_adapters=4,
        description="Maximum redundancy with striped mirrors",
    ),
}


def select_adapters(
    available: list[Adapter],
    *,
    names: list[str] | None = None,
    tags: list[str] | None = None,
) -> list[Adapter]:
    """Filter adapters by name and/or tag.

    If both *names* and *tags* are given, an adapter must match at least
    one name OR have at least one matching tag.  If neither is given,
    all adapters are returned.
    """
    if not names and not tags:
        return list(available)

    selected: list[Adapter] = []
    seen: set[str] = set()

    for adapter in available:
        prof = adapter.profile()
        if prof.name in seen:
            continue

        matched = False
        if names and prof.name in names:
            matched = True
        if tags:
            adapter_tags = set(prof.tags.keys()) | set(prof.tags.values())
            if adapter_tags & set(tags):
                matched = True

        if matched:
            selected.append(adapter)
            seen.add(prof.name)

    return selected


def build_from_profile(
    profile: ScatterProfile,
    available: list[Adapter],
) -> list[Adapter]:
    """Build a RAID-wrapped adapter list from a scatter profile.

    Returns a list containing the RaidPool (if enough adapters match),
    falling back to the matched adapters without RAID wrapping if the
    minimum isn't met.
    """
    candidates = select_adapters(
        available,
        names=profile.adapter_names or None,
        tags=profile.adapter_tags or None,
    )

    if len(candidates) < profile.min_adapters:
        return candidates if candidates else list(available)

    level = RaidLevel(profile.raid_level)

    min_for_level = {
        RaidLevel.RAID0: 2,
        RaidLevel.RAID1: 2,
        RaidLevel.RAID5: 3,
        RaidLevel.RAID10: 4,
    }
    if len(candidates) < min_for_level.get(level, 2):
        return candidates

    if level == RaidLevel.RAID10 and len(candidates) % 2 != 0:
        candidates = candidates[:-1]

    pool = RaidPool(candidates, level, mirror_count=profile.mirror_count)
    return [pool]


def resolve_profile(
    name: str,
    custom_profiles: list[ScatterProfile] | None = None,
) -> ScatterProfile:
    """Look up a scatter profile by name, checking custom first, then builtins."""
    if custom_profiles:
        for p in custom_profiles:
            if p.name == name:
                return p
    if name in BUILTIN_PROFILES:
        return BUILTIN_PROFILES[name]
    raise KeyError(f"unknown scatter profile: {name!r}")


def list_profiles(
    custom_profiles: list[ScatterProfile] | None = None,
) -> list[ScatterProfile]:
    """Return all available profiles (custom overrides builtins)."""
    result: dict[str, ScatterProfile] = dict(BUILTIN_PROFILES)
    if custom_profiles:
        for p in custom_profiles:
            result[p.name] = p
    return list(result.values())
