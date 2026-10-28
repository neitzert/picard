"""Tests for EfiVariableAdapter."""

import sys
import pytest

if sys.platform != "linux":
    pytest.skip("efi-variable adapter requires Linux", allow_module_level=True)

import asyncio
import io
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch, mock_open

from picard.adapters.efi_variable_adapter import EfiVariableAdapter, _EFIVARS_PATH


CHUNK = "PCHK:2:test-manifest:0:0:1:dGVzdHBheWxvYWQ="


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestProfile(unittest.TestCase):
    def test_profile_fields(self):
        a = EfiVariableAdapter()
        p = a.profile()
        assert p.name == "efi-variable"
        assert p.max_chunk_bytes == 8192
        assert p.persistent is True
        assert p.ordered is False
        assert "firmware-persistent-survives-reboot" in p.tags.get("WARNING", "")


class TestHealthCheck(unittest.TestCase):
    def test_returns_true_when_efivars_present(self):
        with patch("os.path.isdir", return_value=True), \
             patch("os.access", return_value=True):
            a = EfiVariableAdapter()
            assert run(a.health_check()) is True

    def test_returns_false_when_efivars_absent(self):
        with patch("os.path.isdir", return_value=False):
            a = EfiVariableAdapter()
            assert run(a.health_check()) is False

    def test_returns_false_when_not_readable(self):
        with patch("os.path.isdir", return_value=True), \
             patch("os.access", return_value=False):
            a = EfiVariableAdapter()
            assert run(a.health_check()) is False


class TestWriteRead(unittest.TestCase):
    def _write_with_mocks(self, a):
        with patch("os.path.isdir", return_value=True), \
             patch("os.path.exists", return_value=False), \
             patch("builtins.open", mock_open()):
            return run(a.write_chunk(CHUNK))

    def test_write_read_roundtrip(self):
        a = EfiVariableAdapter()
        ref = self._write_with_mocks(a)
        assert ref.adapter_name == "efi-variable"
        # read from in-process registry (no real fs)
        with patch("os.path.exists", return_value=False):
            result = run(a.read_chunk(ref))
        assert result == CHUNK

    def test_write_raises_on_oversized_chunk(self):
        a = EfiVariableAdapter(max_chunk_bytes=5)
        with patch("os.path.isdir", return_value=True):
            with pytest.raises(ValueError, match="too large"):
                run(a.write_chunk(CHUNK))

    def test_write_raises_when_efivars_missing(self):
        a = EfiVariableAdapter()
        with patch("os.path.isdir", return_value=False):
            with pytest.raises(RuntimeError, match="EFI variables not available"):
                run(a.write_chunk(CHUNK))

    def test_write_raises_on_enospc(self):
        a = EfiVariableAdapter()
        import errno
        enospc = OSError(errno.ENOSPC, "No space left on device")
        with patch("os.path.isdir", return_value=True), \
             patch("os.path.exists", return_value=False), \
             patch("builtins.open", side_effect=enospc):
            with pytest.raises(RuntimeError, match="EFI variable storage full"):
                run(a.write_chunk(CHUNK))

    def test_read_raises_for_unknown_ref(self):
        from picard.adapters.base import StoredChunkRef
        a = EfiVariableAdapter()
        ref = StoredChunkRef(adapter_name="efi-variable", location="efi-variable://ghost")
        with patch("os.path.exists", return_value=False):
            with pytest.raises(KeyError):
                run(a.read_chunk(ref))

    def test_read_from_filesystem_when_exists(self):
        a = EfiVariableAdapter()
        ref = self._write_with_mocks(a)
        raw = b"\x07\x00\x00\x00" + CHUNK.encode()
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data=raw)):
            result = run(a.read_chunk(ref))
        assert result == CHUNK


class TestDeleteChunk(unittest.TestCase):
    def test_delete_returns_true_when_exists(self):
        a = EfiVariableAdapter()
        with patch("os.path.isdir", return_value=True), \
             patch("os.path.exists", return_value=False), \
             patch("builtins.open", mock_open()):
            ref = run(a.write_chunk(CHUNK))
        a._chattr_i = AsyncMock()
        with patch("os.path.exists", return_value=False):
            assert run(a.delete_chunk(ref)) is True

    def test_delete_prevents_read(self):
        a = EfiVariableAdapter()
        with patch("os.path.isdir", return_value=True), \
             patch("os.path.exists", return_value=False), \
             patch("builtins.open", mock_open()):
            ref = run(a.write_chunk(CHUNK))
        with patch("os.path.exists", return_value=False):
            run(a.delete_chunk(ref))
        with patch("os.path.exists", return_value=False):
            with pytest.raises(KeyError):
                run(a.read_chunk(ref))

    def test_delete_returns_false_when_absent(self):
        from picard.adapters.base import StoredChunkRef
        a = EfiVariableAdapter()
        ref = StoredChunkRef(adapter_name="efi-variable", location="efi-variable://ghost")
        assert run(a.delete_chunk(ref)) is False


class TestListChunks(unittest.TestCase):
    def test_list_chunks(self):
        a = EfiVariableAdapter()
        with patch("os.path.isdir", return_value=True), \
             patch("os.path.exists", return_value=False), \
             patch("builtins.open", mock_open()):
            run(a.write_chunk(CHUNK))
        listed = run(a.list_chunks("test-manifest"))
        assert len(listed) == 1

    def test_list_chunks_filters_by_manifest(self):
        a = EfiVariableAdapter()
        with patch("os.path.isdir", return_value=True), \
             patch("os.path.exists", return_value=False), \
             patch("builtins.open", mock_open()):
            run(a.write_chunk(CHUNK))
        listed = run(a.list_chunks("other-manifest"))
        assert len(listed) == 0


class TestSafetyGuard(unittest.TestCase):
    def test_var_name_always_starts_with_picard(self):
        """Write_chunk always creates picard.* variable names."""
        a = EfiVariableAdapter()
        written_paths = []

        original_open = open

        def tracking_open(path, mode="r", *args, **kwargs):
            if "w" in mode or "b" in mode:
                written_paths.append(path)
            return mock_open()(path, mode, *args, **kwargs)

        with patch("os.path.isdir", return_value=True), \
             patch("os.path.exists", return_value=False), \
             patch("builtins.open", mock_open()):
            run(a.write_chunk(CHUNK))

        for vn in a._registry:
            assert vn.startswith("picard."), f"Non-picard variable name: {vn}"
