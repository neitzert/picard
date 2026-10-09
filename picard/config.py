"""Configuration — load, save, and validate ``~/.picard/config.toml``.

Supports credential fields prefixed with ``keyring:`` that are resolved
from the OS keyring at runtime.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]

DEFAULT_CONFIG_DIR = Path.home() / ".picard"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.toml"
DEFAULT_INDEX_DB = str(DEFAULT_CONFIG_DIR / "index.db")
DEFAULT_BLOCK_SIZE = 65536


VALID_BROWSER_MODES = frozenset({"api", "headless", "headed"})
VALID_RAID_LEVELS = frozenset({"raid0", "raid1", "raid5", "raid10"})
_MAX_ADAPTER_NAME_LEN = 128
_ADAPTER_NAME_BAD = frozenset("/\\\x00..")


@dataclass
class AdapterEntry:
    """One ``[[adapters]]`` stanza from config.toml."""

    name: str
    type: str
    enabled: bool = True
    params: dict[str, Any] = field(default_factory=dict)
    tor_proxy: bool = False
    browser_mode: str = "api"

    def __post_init__(self) -> None:
        if self.browser_mode not in VALID_BROWSER_MODES:
            self.browser_mode = "api"


@dataclass
class RaidEntry:
    """One ``[raid]`` or ``[[raid]]`` stanza from config.toml."""

    name: str = "default"
    level: str = "raid0"
    adapters: list[str] = field(default_factory=list)
    mirror_count: int = 2


@dataclass
class ScatterProfile:
    """One ``[scatter.<name>]`` stanza from config.toml."""

    name: str = "default"
    raid_level: str = "raid0"
    mirror_count: int = 2
    adapter_tags: list[str] = field(default_factory=list)
    adapter_names: list[str] = field(default_factory=list)
    min_adapters: int = 2
    description: str = ""


@dataclass
class PicardConfig:
    """Top-level configuration."""

    index_db: str = DEFAULT_INDEX_DB
    block_size: int = DEFAULT_BLOCK_SIZE
    adapters: list[AdapterEntry] = field(default_factory=list)
    raid: list[RaidEntry] = field(default_factory=list)
    scatter_profiles: list[ScatterProfile] = field(default_factory=list)


def load_config(path: Path | None = None) -> PicardConfig:
    """Load and parse a TOML config file.  Returns defaults if the file doesn't exist."""
    path = path or DEFAULT_CONFIG_PATH
    if not path.is_file():
        return PicardConfig()

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    general = raw.get("general", {})
    block_size = general.get("block_size", DEFAULT_BLOCK_SIZE)
    if not isinstance(block_size, int) or block_size < 1024 or block_size > 64 * 1024 * 1024:
        raise ValueError(
            f"block_size must be between 1024 and 67108864, got {block_size}"
        )
    cfg = PicardConfig(
        index_db=general.get("index_db", DEFAULT_INDEX_DB),
        block_size=block_size,
    )

    seen_names: set[str] = set()
    for entry in raw.get("adapters", []):
        aname = entry["name"]
        if not aname or len(aname) > _MAX_ADAPTER_NAME_LEN:
            raise ValueError(f"adapter name must be 1-{_MAX_ADAPTER_NAME_LEN} chars, got {aname!r}")
        if "\x00" in aname or "/" in aname or "\\" in aname or ".." in aname:
            raise ValueError(f"adapter name contains forbidden characters: {aname!r}")
        if aname in seen_names:
            raise ValueError(f"duplicate adapter name: {aname!r}")
        seen_names.add(aname)
        params = {k: v for k, v in entry.items() if k not in ("name", "type", "enabled", "tor_proxy", "browser_mode")}
        cfg.adapters.append(
            AdapterEntry(
                name=aname,
                type=entry["type"],
                enabled=entry.get("enabled", True),
                params=params,
                tor_proxy=entry.get("tor_proxy", False),
                browser_mode=entry.get("browser_mode", "api"),
            )
        )

    raid_raw = raw.get("raid", None)
    if raid_raw is not None:
        if isinstance(raid_raw, dict):
            raid_raw = [raid_raw]
        for i, entry in enumerate(raid_raw):
            level = entry.get("level", "raid0")
            if level not in VALID_RAID_LEVELS:
                raise ValueError(
                    f"invalid RAID level {level!r}, must be one of {sorted(VALID_RAID_LEVELS)}"
                )
            cfg.raid.append(
                RaidEntry(
                    name=entry.get("name", f"pool-{i}"),
                    level=level,
                    adapters=entry.get("adapters", []),
                    mirror_count=entry.get("mirror_count", 2),
                )
            )

    for name, profile_raw in raw.get("scatter", {}).items():
        if isinstance(profile_raw, dict):
            cfg.scatter_profiles.append(
                ScatterProfile(
                    name=name,
                    raid_level=profile_raw.get("raid_level", "raid0"),
                    mirror_count=profile_raw.get("mirror_count", 2),
                    adapter_tags=profile_raw.get("adapter_tags", []),
                    adapter_names=profile_raw.get("adapter_names", []),
                    min_adapters=profile_raw.get("min_adapters", 2),
                    description=profile_raw.get("description", ""),
                )
            )

    return cfg


def resolve_keyring(value: str) -> str:
    """If *value* starts with ``keyring:``, look it up in the OS keyring.

    The format is ``keyring:service/username``.  Otherwise return *value* as-is.
    """
    if not isinstance(value, str) or not value.startswith("keyring:"):
        return value

    import keyring as kr

    parts = value[len("keyring:") :].rsplit("/", 1)
    if len(parts) != 2:
        raise ValueError(f"bad keyring ref (need 'keyring:service/key'): {value!r}")
    service, key = parts
    secret = kr.get_password(service, key)
    if secret is None:
        raise LookupError(f"keyring entry not found: service={service!r}, key={key!r}")
    return secret


def resolve_params(params: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *params* with all ``keyring:`` values resolved."""
    return {k: resolve_keyring(v) if isinstance(v, str) else v for k, v in params.items()}


def mask_secrets(params: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *params* with ``keyring:`` values replaced by ``***``."""
    return {
        k: "***" if isinstance(v, str) and v.startswith("keyring:") else v
        for k, v in params.items()
    }


def init_config(path: Path | None = None) -> Path:
    """Create a default config.toml if one doesn't exist.  Returns the path."""
    path = path or DEFAULT_CONFIG_PATH
    if path.is_file():
        raise FileExistsError(f"config already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _DEFAULT_TOML,
        encoding="utf-8",
    )
    return path


_DEFAULT_TOML = """\
[general]
index_db = "~/.picard/index.db"
block_size = 65536

# Add adapters below.  Example:
#
# [[adapters]]
# name = "local"
# type = "filesystem"
# enabled = true
# root_dir = "~/.picard/chunks"
#
# [[adapters]]
# name = "my-bluesky"
# type = "bluesky"
# handle = "you.bsky.social"
# app_password = "keyring:picard/my-bluesky/app_password"

# RAID pool — wrap named adapters in a redundancy pool.
# Levels: raid0 (stripe), raid1 (mirror), raid5 (parity), raid10 (striped mirrors)
#
# [raid]
# name = "default"
# level = "raid1"
# mirror_count = 2
# adapters = ["local", "my-bluesky"]

# Scatter profiles — named presets for adapter selection and RAID level.
#
# [scatter.stealth]
# description = "Low-visibility scatter across unconventional protocols"
# raid_level = "raid1"
# mirror_count = 2
# adapter_tags = ["steganographic", "low-profile"]
# min_adapters = 4
#
# [scatter.resilient]
# description = "Maximum redundancy across reliable adapters"
# raid_level = "raid5"
# adapter_tags = ["persistent", "cloud"]
# min_adapters = 5
#
# [scatter.speed]
# description = "Fast local scatter with no redundancy"
# raid_level = "raid0"
# adapter_tags = ["local", "fast"]
# min_adapters = 2
"""
