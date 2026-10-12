"""Tests for the git protocol adapter."""

from unittest.mock import AsyncMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.git_protocol import GitProtocolAdapter


@pytest.fixture
def adapter():
    return GitProtocolAdapter("/tmp/picard-bare.git")


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "git-protocol"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 1_048_576

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 500


@pytest.mark.asyncio
class TestWrite:
    async def test_write_returns_ref(self, adapter):
        mock_hash_proc = AsyncMock()
        mock_hash_proc.communicate = AsyncMock(return_value=(b"abc123def456\n", b""))
        mock_hash_proc.returncode = 0

        mock_notes_proc = AsyncMock()
        mock_notes_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_notes_proc.returncode = 0

        call_count = 0

        async def mock_exec(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mock_hash_proc
            return mock_notes_proc

        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
            ref = await adapter.write_chunk("PCHK:man1:0:0:hello")

        assert ref.adapter_name == "git-protocol"
        assert "git:///tmp/picard-bare.git/abc123def456" == ref.location


@pytest.mark.asyncio
class TestRead:
    async def test_read_returns_data(self, adapter):
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(
            return_value=(b"PCHK:man1:0:0:hello", b"")
        )
        mock_proc.returncode = 0

        ref = StoredChunkRef(
            adapter_name="git-protocol",
            location="git:///tmp/picard-bare.git/abc123def456",
        )

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
            result = await adapter.read_chunk(ref)

        assert result == "PCHK:man1:0:0:hello"

    async def test_read_not_found(self, adapter):
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"not found"))
        mock_proc.returncode = 128

        ref = StoredChunkRef(
            adapter_name="git-protocol",
            location="git:///tmp/picard-bare.git/deadbeef",
        )

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
            with pytest.raises(KeyError):
                await adapter.read_chunk(ref)


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_success(self, adapter):
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        ref = StoredChunkRef(
            adapter_name="git-protocol",
            location="git:///tmp/picard-bare.git/abc123",
        )

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
            assert await adapter.delete_chunk(ref) is True

    async def test_delete_not_found(self, adapter):
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"no note"))
        mock_proc.returncode = 1

        ref = StoredChunkRef(
            adapter_name="git-protocol",
            location="git:///tmp/picard-bare.git/deadbeef",
        )

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
            assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestHealth:
    async def test_health_ok(self, adapter):
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b".", b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
            assert await adapter.health_check() is True

    async def test_health_fail(self, adapter):
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"not a git repo"))
        mock_proc.returncode = 128

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
            assert await adapter.health_check() is False
