"""Tests for the SMB/CIFS adapter -- mocked, no real SMB server."""

from __future__ import annotations

import sys
from io import StringIO
from unittest.mock import MagicMock, patch

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


@pytest.fixture
def mock_smbclient():
    mock = MagicMock()
    with patch.dict(sys.modules, {"smbclient": mock}):
        yield mock


@pytest.fixture
def adapter(mock_smbclient):
    from picard.adapters.smb import SmbAdapter

    return SmbAdapter(
        server="fileserver", share="data", username="user", password="pw"
    )


@pytest.mark.asyncio
class TestSmbAdapter:
    async def test_profile(self, adapter):
        p = adapter.profile()
        assert p.name == "smb"
        assert p.max_chunk_bytes == 65536
        assert p.persistent is True
        assert p.rate_limit_per_min == 1000

    async def test_profile_custom_max(self, mock_smbclient):
        from picard.adapters.smb import SmbAdapter

        a = SmbAdapter(
            server="s", share="sh", username="u", password="p",
            max_chunk_bytes=1024,
        )
        assert a.profile().max_chunk_bytes == 1024

    async def test_write_chunk(self, adapter, mock_smbclient):
        mock_smbclient.mkdir = MagicMock()
        mock_file = StringIO()
        mock_smbclient.open_file = MagicMock(return_value=MagicMock(
            __enter__=MagicMock(return_value=mock_file),
            __exit__=MagicMock(return_value=False),
        ))

        enc = _sample_encoded()
        ref = await adapter.write_chunk(enc)

        assert ref.adapter_name == "smb"
        assert "smb://" in ref.location
        assert "m1" in ref.location
        assert ref.location.endswith(".pchk")

    async def test_write_opaque_chunk(self, adapter, mock_smbclient):
        mock_smbclient.mkdir = MagicMock()
        mock_smbclient.open_file = MagicMock(return_value=MagicMock(
            __enter__=MagicMock(return_value=StringIO()),
            __exit__=MagicMock(return_value=False),
        ))

        ref = await adapter.write_chunk("not-a-valid-chunk-format")
        assert ref.adapter_name == "smb"
        assert "smb://" in ref.location

    async def test_read_chunk(self, adapter, mock_smbclient):
        enc = _sample_encoded()
        reader = StringIO(enc)
        mock_smbclient.open_file = MagicMock(return_value=MagicMock(
            __enter__=MagicMock(return_value=reader),
            __exit__=MagicMock(return_value=False),
        ))

        ref = StoredChunkRef(
            adapter_name="smb",
            location="smb://fileserver/data/m1/0_0.pchk",
        )
        result = await adapter.read_chunk(ref)
        assert result == enc

    async def test_delete_chunk(self, adapter, mock_smbclient):
        mock_smbclient.remove = MagicMock()

        ref = StoredChunkRef(
            adapter_name="smb",
            location="smb://fileserver/data/m1/0_0.pchk",
        )
        assert await adapter.delete_chunk(ref) is True

    async def test_delete_chunk_not_found(self, adapter, mock_smbclient):
        mock_smbclient.remove = MagicMock(side_effect=OSError("not found"))

        ref = StoredChunkRef(
            adapter_name="smb",
            location="smb://fileserver/data/m1/0_0.pchk",
        )
        assert await adapter.delete_chunk(ref) is False

    async def test_list_chunks(self, adapter, mock_smbclient):
        entry1 = MagicMock()
        entry1.name = "0_0.pchk"
        entry2 = MagicMock()
        entry2.name = "0_1.pchk"
        entry3 = MagicMock()
        entry3.name = "readme.txt"
        mock_smbclient.scandir = MagicMock(return_value=[entry1, entry2, entry3])

        refs = await adapter.list_chunks("m1")
        assert len(refs) == 2
        assert all(r.adapter_name == "smb" for r in refs)

    async def test_list_chunks_empty(self, adapter, mock_smbclient):
        mock_smbclient.scandir = MagicMock(side_effect=OSError("no dir"))

        refs = await adapter.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self, adapter, mock_smbclient):
        mock_smbclient.scandir = MagicMock(return_value=[])

        assert await adapter.health_check() is True

    async def test_health_check_fail(self, adapter, mock_smbclient):
        mock_smbclient.scandir = MagicMock(side_effect=Exception("conn refused"))

        assert await adapter.health_check() is False

    async def test_use_ads_location(self, mock_smbclient):
        from picard.adapters.smb import SmbAdapter

        a = SmbAdapter(
            server="s", share="sh", username="u", password="p", use_ads=True,
        )
        assert a._use_ads is True
