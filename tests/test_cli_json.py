"""Tests for CLI --json output and formatting."""

import json

import pytest
from typer.testing import CliRunner

from picard.cli import app

runner = CliRunner()


class TestCliJson:
    def test_adapters_json(self):
        result = runner.invoke(app, ["adapters", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) > 0

    def test_adapters_plain(self):
        result = runner.invoke(app, ["adapters"])
        assert result.exit_code == 0
        assert "Registered Adapters" in result.output

    def test_profiles_json(self):
        result = runner.invoke(app, ["profiles", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        for p in data:
            assert "name" in p
            assert "raid_level" in p

    def test_profiles_plain(self):
        result = runner.invoke(app, ["profiles"])
        assert result.exit_code == 0
        assert "Scatter Profiles" in result.output
