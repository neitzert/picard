"""Tests for new CLI filesystem commands via CliRunner."""

from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from picard.cli import app

runner = CliRunner()


@pytest.fixture
def tmp_picard(tmp_path):
    store = tmp_path / "chunks"
    store.mkdir()
    db = tmp_path / "index.db"
    return store, db


def _run(cmd_args, store, db, passphrase="testpw"):
    args = list(cmd_args) + ["--store", str(store), "--db", str(db)]
    with patch("picard.cli._get_passphrase", return_value=passphrase):
        return runner.invoke(app, args)


class TestMkdir:
    def test_mkdir(self, tmp_picard):
        store, db = tmp_picard
        result = _run(["mkdir", "/docs"], store, db)
        assert result.exit_code == 0
        assert "mkdir" in result.output

    def test_mkdir_parents(self, tmp_picard):
        store, db = tmp_picard
        result = _run(["mkdir", "-p", "/a/b/c"], store, db)
        assert result.exit_code == 0


class TestStatAfterMkdir:
    def test_stat_directory(self, tmp_picard):
        store, db = tmp_picard
        _run(["mkdir", "/mydir"], store, db)
        result = _run(["stat", "/mydir"], store, db)
        assert result.exit_code == 0
        assert "directory" in result.output

    def test_stat_missing(self, tmp_picard):
        store, db = tmp_picard
        result = _run(["stat", "/nope"], store, db)
        assert result.exit_code != 0


class TestTree:
    def test_tree_empty(self, tmp_picard):
        store, db = tmp_picard
        result = _run(["tree"], store, db)
        assert result.exit_code == 0

    def test_tree_with_dirs(self, tmp_picard):
        store, db = tmp_picard
        _run(["mkdir", "-p", "/a/b"], store, db)
        result = _run(["tree"], store, db)
        assert result.exit_code == 0


class TestFind:
    def test_find_dirs(self, tmp_picard):
        store, db = tmp_picard
        _run(["mkdir", "-p", "/docs/api"], store, db)
        result = _run(["find", "/docs"], store, db)
        assert result.exit_code == 0
        assert "/docs" in result.output


class TestLn:
    def test_ln(self, tmp_picard):
        store, db = tmp_picard
        _run(["mkdir", "/target_dir"], store, db)
        result = _run(["ln", "/target_dir", "/link_dir"], store, db)
        assert result.exit_code == 0
        assert "->" in result.output


class TestLog:
    def test_log_no_versions(self, tmp_picard):
        store, db = tmp_picard
        result = _run(["log", "/nope"], store, db)
        assert result.exit_code == 0
        assert "no versions" in result.output


class TestFsck:
    def test_fsck_clean(self, tmp_picard):
        store, db = tmp_picard
        result = _run(["fsck", "--no-digests"], store, db)
        assert result.exit_code == 0
        assert "OK" in result.output

    def test_fsck_after_mkdir(self, tmp_picard):
        store, db = tmp_picard
        _run(["mkdir", "/checked"], store, db)
        result = _run(["fsck", "--no-digests"], store, db)
        assert result.exit_code == 0
        assert "1 manifests" in result.output
