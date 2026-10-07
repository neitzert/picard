"""Tests for CLI commands: adapters, demo, init."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from picard.cli import app

runner = CliRunner()


class TestAdaptersCommand:
    def test_lists_all(self):
        result = runner.invoke(app, ["adapters"])
        assert result.exit_code == 0
        assert "259" in result.output
        for name in ("dns", "mqtt", "filesystem", "memory", "redis", "s3"):
            assert name in result.output

    def test_verbose_shows_class_names(self):
        result = runner.invoke(app, ["adapters", "--verbose"])
        assert result.exit_code == 0
        assert "Adapter" in result.output


@pytest.mark.timeout(120)
class TestDemoCommand:
    @pytest.fixture(autouse=True, scope="class")
    def demo_result(self):
        TestDemoCommand._result = runner.invoke(app, ["demo", "--adapters", "3"])

    def test_exit_code(self):
        assert self._result.exit_code == 0, self._result.output

    def test_banner(self):
        assert "PICARD" in self._result.output

    def test_protocol_count(self):
        assert "3 protocols" in self._result.output

    def test_reassembly(self):
        assert "Reassembly successful" in self._result.output

    def test_attack_surface_report(self):
        assert "ATTACK SURFACE REPORT" in self._result.output

    def test_tagline(self):
        assert "Platform security is not protocol security" in self._result.output


@pytest.mark.timeout(120)
class TestDemoWithFile:
    def test_scatter_real_file(self, tmp_path):
        f = tmp_path / "hello.txt"
        f.write_bytes(b"hello from picard")
        result = runner.invoke(app, ["demo", str(f), "--adapters", "3"])
        assert result.exit_code == 0, result.output
        assert "Reassembly successful" in result.output
        assert "hello.txt" in result.output


class TestInitCommand:
    def test_creates_config(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "config.toml"
        monkeypatch.setattr("picard.config.DEFAULT_CONFIG_PATH", cfg_path)
        monkeypatch.setattr("picard.cli.DEFAULT_CONFIG_PATH", cfg_path)
        result = runner.invoke(app, ["init"])
        assert result.exit_code == 0, result.output
        assert cfg_path.is_file()

    def test_already_exists(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text("[general]\n")
        monkeypatch.setattr("picard.config.DEFAULT_CONFIG_PATH", cfg_path)
        monkeypatch.setattr("picard.cli.DEFAULT_CONFIG_PATH", cfg_path)
        result = runner.invoke(app, ["init"])
        assert result.exit_code == 1
