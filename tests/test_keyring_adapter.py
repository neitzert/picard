"""Unit tests for keyring_adapter (Linux-only)."""
import sys
import pytest

if sys.platform != "linux":
    pytest.skip("keyring_adapter requires Linux", allow_module_level=True)

import asyncio
from unittest.mock import patch, AsyncMock, MagicMock
from picard.adapters.keyring_adapter import KeyringAdapter
from picard.adapters.base import StoredChunkRef

FAKE_CHUNK = "PCHK:1:keymid:0:0:1:aGVsbG8="


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _mock_proc(stdout=b"", returncode=0):
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = b""
    return proc


def test_profile():
    a = KeyringAdapter()
    p = a.profile()
    assert p.name == "kernel-keyring"
    assert p.persistent is False
    assert p.max_chunk_bytes == 32767


def test_write_read_delete():
    a = KeyringAdapter()
    key_id = "123456789"
    with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
        async def _run_write():
            proc = _mock_proc(stdout=(key_id + "\n").encode())
            mock_exec.return_value = proc
            proc.communicate = AsyncMock(return_value=(proc.stdout, proc.stderr))
            return await a.write_chunk(FAKE_CHUNK)
        ref = _run(_run_write())

    assert ref.adapter_name == "kernel-keyring"
    assert key_id in ref.location

    with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
        async def _run_read():
            proc = _mock_proc(stdout=FAKE_CHUNK.encode())
            mock_exec.return_value = proc
            proc.communicate = AsyncMock(return_value=(proc.stdout, proc.stderr))
            return await a.read_chunk(ref)
        data = _run(_run_read())
    assert data == FAKE_CHUNK

    with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
        async def _run_del():
            proc = _mock_proc()
            mock_exec.return_value = proc
            proc.communicate = AsyncMock(return_value=(proc.stdout, proc.stderr))
            return await a.delete_chunk(ref)
        deleted = _run(_run_del())
    assert deleted is True


def test_health_check():
    a = KeyringAdapter()
    with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
        async def _run_health():
            proc = _mock_proc(stdout=b"123\n")
            mock_exec.return_value = proc
            proc.communicate = AsyncMock(return_value=(proc.stdout, proc.stderr))
            return await a.health_check()
        result = _run(_run_health())
    assert result is True
