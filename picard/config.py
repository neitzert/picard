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


@dataclass
class AdapterEntry:
    """One ``[[adapters]]`` stanza from config.toml."""

    name: str
    type: str
    enabled: bool = True
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class PicardConfig:
    """Top-level configuration."""

    index_db: str = DEFAULT_INDEX_DB
    block_size: int = DEFAULT_BLOCK_SIZE
    adapters: list[AdapterEntry] = field(default_factory=list)


def load_config(path: Path | None = None) -> PicardConfig:
    """Load and parse a TOML config file.  Returns defaults if the file doesn't exist."""
    path = path or DEFAULT_CONFIG_PATH
    if not path.is_file():
        return PicardConfig()

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    general = raw.get("general", {})
    cfg = PicardConfig(
        index_db=general.get("index_db", DEFAULT_INDEX_DB),
        block_size=general.get("block_size", DEFAULT_BLOCK_SIZE),
    )

    for entry in raw.get("adapters", []):
        params = {k: v for k, v in entry.items() if k not in ("name", "type", "enabled")}
        cfg.adapters.append(
            AdapterEntry(
                name=entry["name"],
                type=entry["type"],
                enabled=entry.get("enabled", True),
                params=params,
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
"""
