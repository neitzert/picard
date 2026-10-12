"""Tests for the SSH/SFTP adapter -- mocked, no real SSH server."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.chunker import Chunk


def _sample_encoded(manifest_id: str = "m1", block: int = 0, idx: int = 0) -> str:
    return Chunk(
        version=1,
        manifest_id=manifest_id,
        block_index=block,
        chunk_index=idx,
        total_chunks=1,
        payload=b"test-payload",
    ).encode()


def _mock_sftp(read_data: str = ""):
    sftp = AsyncMock()
    sftp.mkdir = AsyncMock()

    mock_file = AsyncMock()
    mock_file.write = AsyncMock()
    mock_file.read = AsyncMock(return_value=read_data)
    mock_file.__aenter__ = AsyncMock(return_value=mock_file)
    mock_file.__aexit__ = AsyncMock(return_value=False)
    sftp.open = MagicMock(return_value=mock_file)

    sftp.remove = AsyncMock()
    sftp.listdir = AsyncMock(return_value=[])

    sftp.__aenter__ = AsyncMock(return_value=sftp)
    sftp.__aexit__ = AsyncMock(return_value=False)
    return sftp


def _mock_conn(sftp=None):
    conn = AsyncMock()
    if sftp is None:
        sftp = _mock_sftp()
    conn.start_sftp_client = MagicMock(return_value=sftp)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)

    result = MagicMock()
    result.stdout = "ok"
    conn.run = AsyncMock(return_value=result)

    return conn


@pytest.fixture
def mock_asyncssh():
    mock = MagicMock()
    mock.connect = AsyncMock()
    with patch.dict(sys.modules, {"asyncssh": mock}):
        yield mock


@pytest.fixture
def adapter(mock_asyncssh):
    from picard.adapters.ssh_adapter import SshAdapter

    return SshAdapter(host="remote.host", username="user")


@pytest.mark.asyncio
class TestSshAdapter:
    async def test_profile(self, adapter):
        p = adapter.profile()
        assert p.name == "ssh"
        assert p.max_chunk_bytes == 1_048_576
        assert p.persistent is True
        assert p.rate_limit_per_min == 300

    async def test_profile_custom(self, mock_asyncssh):
        from picard.adapters.ssh_adapter import SshAdapter

        a = SshAdapter(host="h", username="u", max_chunk_bytes=4096, port=2222)
        assert a.profile().max_chunk_bytes == 4096
        assert a._port == 2222

    async def test_write_chunk(self, adapter, mock_asyncssh):
        conn = _mock_conn()
        mock_asyncssh.connect = AsyncMock(return_value=conn)

        enc = _sample_encoded()
        ref = await adapter.write_chunk(enc)

        assert ref.adapter_name == "ssh"
        assert "ssh://" in ref.location
        assert "m1" in ref.location

    async def test_write_opaque_chunk(self, adapter, mock_asyncssh):
        conn = _mock_conn()
        mock_asyncssh.connect = AsyncMock(return_value=conn)

        ref = await adapter.write_chunk("opaque-sealed-data")
        assert ref.adapter_name == "ssh"

    async def test_read_chunk(self, adapter, mock_asyncssh):
        enc = _sample_encoded()
        sftp = _mock_sftp(read_data=enc)
        conn = _mock_conn(sftp=sftp)
        mock_asyncssh.connect = AsyncMock(return_value=conn)

        ref = StoredChunkRef(
            adapter_name="ssh",
            location="ssh://remote.host/tmp/.picard/m1/0_0.pchk",
        )
        result = await adapter.read_chunk(ref)
        assert result == enc

    async def test_delete_chunk(self, adapter, mock_asyncssh):
        conn = _mock_conn()
        mock_asyncssh.connect = AsyncMock(return_value=conn)

        ref = StoredChunkRef(
            adapter_name="ssh",
            location="ssh://remote.host/tmp/.picard/m1/0_0.pchk",
        )
        assert await adapter.delete_chunk(ref) is True

    async def test_delete_not_found(self, adapter, mock_asyncssh):
        conn = _mock_conn()
        sftp = _mock_sftp()
        sftp.remove = AsyncMock(side_effect=OSError("not found"))
        conn.start_sftp_client = MagicMock(return_value=sftp)
        mock_asyncssh.connect = AsyncMock(return_value=conn)

        ref = StoredChunkRef(
            adapter_name="ssh",
            location="ssh://remote.host/tmp/.picard/m1/0_0.pchk",
        )
        assert await adapter.delete_chunk(ref) is False

    async def test_list_chunks(self, adapter, mock_asyncssh):
        sftp = _mock_sftp()
        sftp.listdir = AsyncMock(return_value=["0_0.pchk", "0_1.pchk", "readme.txt"])
        conn = _mock_conn(sftp=sftp)
        mock_asyncssh.connect = AsyncMock(return_value=conn)

        refs = await adapter.list_chunks("m1")
        assert len(refs) == 2
        assert all(r.adapter_name == "ssh" for r in refs)

    async def test_list_chunks_empty(self, adapter, mock_asyncssh):
        mock_asyncssh.connect = AsyncMock(side_effect=Exception("conn refused"))

        refs = await adapter.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self, adapter, mock_asyncssh):
        conn = _mock_conn()
        mock_asyncssh.connect = AsyncMock(return_value=conn)

        assert await adapter.health_check() is True

    async def test_health_check_fail(self, adapter, mock_asyncssh):
        mock_asyncssh.connect = AsyncMock(side_effect=Exception("refused"))

        assert await adapter.health_check() is False

    async def test_capacity(self, adapter, mock_asyncssh):
        sftp = _mock_sftp()
        stat = MagicMock()
        stat.f_frsize = 4096
        stat.f_blocks = 1000000
        stat.f_bavail = 500000
        sftp.statvfs = AsyncMock(return_value=stat)
        conn = _mock_conn(sftp=sftp)
        mock_asyncssh.connect = AsyncMock(return_value=conn)

        cap = await adapter.capacity()
        assert cap["total_bytes"] == 4096 * 1000000
        assert cap["free_bytes"] == 4096 * 500000
        assert cap["used_bytes"] == 4096 * 500000
