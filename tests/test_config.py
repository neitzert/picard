"""Tests for config.py — load, save, keyring resolution, masking."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from picard.config import (
    AdapterEntry,
    PicardConfig,
    init_config,
    load_config,
    mask_secrets,
    resolve_keyring,
    resolve_params,
)


@pytest.fixture
def config_dir(tmp_path):
    return tmp_path / ".picard"


@pytest.fixture
def config_file(config_dir):
    config_dir.mkdir()
    path = config_dir / "config.toml"
    path.write_text(
        '[general]\n'
        'index_db = "~/.picard/index.db"\n'
        'block_size = 32768\n'
        '\n'
        '[[adapters]]\n'
        'name = "local"\n'
        'type = "filesystem"\n'
        'enabled = true\n'
        'root_dir = "/tmp/chunks"\n'
        '\n'
        '[[adapters]]\n'
        'name = "my-slack"\n'
        'type = "slack"\n'
        'enabled = false\n'
        'bot_token = "keyring:picard/slack/bot_token"\n',
        encoding="utf-8",
    )
    return path


class TestLoadConfig:
    def test_defaults_when_missing(self, tmp_path):
        cfg = load_config(tmp_path / "nope.toml")
        assert cfg.block_size == 65536
        assert cfg.adapters == []

    def test_loads_general(self, config_file):
        cfg = load_config(config_file)
        assert cfg.block_size == 32768

    def test_loads_adapters(self, config_file):
        cfg = load_config(config_file)
        assert len(cfg.adapters) == 2
        assert cfg.adapters[0].name == "local"
        assert cfg.adapters[0].type == "filesystem"
        assert cfg.adapters[0].enabled is True
        assert cfg.adapters[0].params["root_dir"] == "/tmp/chunks"

    def test_disabled_adapter(self, config_file):
        cfg = load_config(config_file)
        assert cfg.adapters[1].enabled is False


class TestResolveKeyring:
    def test_non_keyring_passthrough(self):
        assert resolve_keyring("plain-value") == "plain-value"

    def test_non_string_passthrough(self):
        assert resolve_keyring(42) == 42

    def test_bad_format_raises(self):
        with pytest.raises(ValueError, match="bad keyring ref"):
            resolve_keyring("keyring:no-slash")

    def test_missing_entry_raises(self):
        with patch("keyring.get_password", return_value=None):
            with pytest.raises(LookupError, match="not found"):
                resolve_keyring("keyring:svc/key")

    def test_success(self):
        with patch("keyring.get_password", return_value="secret123"):
            assert resolve_keyring("keyring:svc/key") == "secret123"


class TestResolveParams:
    def test_resolves_keyring_values(self):
        with patch("keyring.get_password", return_value="s3cr3t"):
            result = resolve_params({"token": "keyring:svc/tok", "url": "http://x"})
            assert result["token"] == "s3cr3t"
            assert result["url"] == "http://x"

    def test_non_string_values(self):
        result = resolve_params({"port": 8080, "enabled": True})
        assert result == {"port": 8080, "enabled": True}


class TestMaskSecrets:
    def test_masks_keyring(self):
        result = mask_secrets({"token": "keyring:svc/tok", "url": "http://x"})
        assert result["token"] == "***"
        assert result["url"] == "http://x"

    def test_non_keyring_unchanged(self):
        result = mask_secrets({"key": "plain"})
        assert result["key"] == "plain"


class TestInitConfig:
    def test_creates_file(self, tmp_path):
        path = tmp_path / "config.toml"
        result = init_config(path)
        assert result == path
        assert path.is_file()
        content = path.read_text(encoding="utf-8")
        assert "[general]" in content
        assert "block_size" in content

    def test_raises_if_exists(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text("existing", encoding="utf-8")
        with pytest.raises(FileExistsError):
            init_config(path)


class TestAdapterNameValidation:
    def _write_cfg(self, tmp_path, adapters_toml: str) -> Path:
        p = tmp_path / "config.toml"
        p.write_text(
            "[general]\nblock_size = 65536\n\n" + adapters_toml,
            encoding="utf-8",
        )
        return p

    def test_empty_name_rejected(self, tmp_path):
        p = self._write_cfg(tmp_path, '[[adapters]]\nname = ""\ntype = "memory"\n')
        with pytest.raises(ValueError, match="1-128"):
            load_config(p)

    def test_slash_rejected(self, tmp_path):
        p = self._write_cfg(tmp_path, '[[adapters]]\nname = "a/b"\ntype = "memory"\n')
        with pytest.raises(ValueError, match="forbidden"):
            load_config(p)

    def test_backslash_rejected(self, tmp_path):
        p = self._write_cfg(tmp_path, '[[adapters]]\nname = "a\\\\b"\ntype = "memory"\n')
        with pytest.raises(ValueError, match="forbidden"):
            load_config(p)

    def test_dotdot_rejected(self, tmp_path):
        p = self._write_cfg(tmp_path, '[[adapters]]\nname = "a..b"\ntype = "memory"\n')
        with pytest.raises(ValueError, match="forbidden"):
            load_config(p)

    def test_too_long_rejected(self, tmp_path):
        long_name = "x" * 129
        p = self._write_cfg(tmp_path, f'[[adapters]]\nname = "{long_name}"\ntype = "memory"\n')
        with pytest.raises(ValueError, match="1-128"):
            load_config(p)

    def test_duplicate_name_rejected(self, tmp_path):
        p = self._write_cfg(
            tmp_path,
            '[[adapters]]\nname = "dup"\ntype = "memory"\n\n'
            '[[adapters]]\nname = "dup"\ntype = "filesystem"\n',
        )
        with pytest.raises(ValueError, match="duplicate"):
            load_config(p)

    def test_valid_name_accepted(self, tmp_path):
        p = self._write_cfg(tmp_path, '[[adapters]]\nname = "my-adapter-1"\ntype = "memory"\n')
        cfg = load_config(p)
        assert cfg.adapters[0].name == "my-adapter-1"


class TestRaidLevelValidation:
    def test_invalid_level_rejected(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(
            "[general]\nblock_size = 65536\n\n"
            '[raid]\nlevel = "raid99"\nadapters = ["a","b"]\n',
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="invalid RAID level"):
            load_config(p)

    def test_valid_levels_accepted(self, tmp_path):
        for level in ("raid0", "raid1", "raid5", "raid10"):
            p = tmp_path / f"config_{level}.toml"
            p.write_text(
                "[general]\nblock_size = 65536\n\n"
                f'[raid]\nlevel = "{level}"\nadapters = ["a","b","c"]\n',
                encoding="utf-8",
            )
            cfg = load_config(p)
            assert cfg.raid[0].level == level


class TestBlockSizeValidation:
    def test_too_small_rejected(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text("[general]\nblock_size = 512\n", encoding="utf-8")
        with pytest.raises(ValueError, match="1024"):
            load_config(p)

    def test_too_large_rejected(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text("[general]\nblock_size = 100000000\n", encoding="utf-8")
        with pytest.raises(ValueError, match="67108864"):
            load_config(p)
