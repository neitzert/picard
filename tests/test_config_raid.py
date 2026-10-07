"""Tests for RAID and scatter profile config parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from picard.config import load_config, RaidEntry, ScatterProfile


def _write_config(tmp_path: Path, content: str) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(content, encoding="utf-8")
    return cfg


class TestRaidConfig:
    def test_no_raid_section(self, tmp_path):
        cfg = load_config(_write_config(tmp_path, "[general]\n"))
        assert cfg.raid == []

    def test_single_raid_section(self, tmp_path):
        cfg = load_config(_write_config(tmp_path, """\
[general]
[raid]
name = "primary"
level = "raid1"
mirror_count = 3
adapters = ["fs-a", "fs-b", "fs-c"]
"""))
        assert len(cfg.raid) == 1
        r = cfg.raid[0]
        assert r.name == "primary"
        assert r.level == "raid1"
        assert r.mirror_count == 3
        assert r.adapters == ["fs-a", "fs-b", "fs-c"]

    def test_raid_defaults(self, tmp_path):
        cfg = load_config(_write_config(tmp_path, """\
[general]
[raid]
adapters = ["a", "b"]
"""))
        r = cfg.raid[0]
        assert r.name == "pool-0"
        assert r.level == "raid0"
        assert r.mirror_count == 2

    def test_multiple_raid_pools(self, tmp_path):
        cfg = load_config(_write_config(tmp_path, """\
[general]
[[raid]]
name = "fast"
level = "raid0"
adapters = ["a", "b"]

[[raid]]
name = "safe"
level = "raid5"
adapters = ["c", "d", "e"]
"""))
        assert len(cfg.raid) == 2
        assert cfg.raid[0].name == "fast"
        assert cfg.raid[1].level == "raid5"


class TestScatterConfig:
    def test_no_scatter_section(self, tmp_path):
        cfg = load_config(_write_config(tmp_path, "[general]\n"))
        assert cfg.scatter_profiles == []

    def test_scatter_profiles(self, tmp_path):
        cfg = load_config(_write_config(tmp_path, """\
[general]

[scatter.stealth]
description = "Low visibility"
raid_level = "raid1"
mirror_count = 3
adapter_tags = ["steganographic"]
min_adapters = 4

[scatter.speed]
description = "Fast local"
raid_level = "raid0"
adapter_names = ["fs-a", "fs-b"]
min_adapters = 2
"""))
        assert len(cfg.scatter_profiles) == 2
        stealth = next(p for p in cfg.scatter_profiles if p.name == "stealth")
        assert stealth.raid_level == "raid1"
        assert stealth.mirror_count == 3
        assert stealth.adapter_tags == ["steganographic"]
        assert stealth.min_adapters == 4

        speed = next(p for p in cfg.scatter_profiles if p.name == "speed")
        assert speed.raid_level == "raid0"
        assert speed.adapter_names == ["fs-a", "fs-b"]

    def test_scatter_defaults(self, tmp_path):
        cfg = load_config(_write_config(tmp_path, """\
[general]
[scatter.minimal]
description = "bare minimum"
"""))
        p = cfg.scatter_profiles[0]
        assert p.name == "minimal"
        assert p.raid_level == "raid0"
        assert p.mirror_count == 2
        assert p.adapter_tags == []
        assert p.adapter_names == []
        assert p.min_adapters == 2
