"""Tests for the POP3 adapter."""

from unittest.mock import MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.pop3_adapter import Pop3Adapter


@pytest.fixture
def adapter():
    return Pop3Adapter(
        "mail.example.com",
        "user@example.com",
        "password123",
        smtp_host="smtp.example.com",
    )


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "pop3"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 50_000

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 30


@pytest.mark.asyncio
class TestWrite:
    async def test_write_returns_ref(self, adapter):
        mock_smtp = MagicMock()
        mock_smtp.__enter__ = MagicMock(return_value=mock_smtp)
        mock_smtp.__exit__ = MagicMock(return_value=False)

        with patch("smtplib.SMTP", return_value=mock_smtp):
            ref = await adapter.write_chunk("PCHK:test:0:0:data")

        assert ref.adapter_name == "pop3"
        assert "pop3://mail.example.com/" in ref.location
        mock_smtp.starttls.assert_called_once()
        mock_smtp.login.assert_called_once()
        mock_smtp.send_message.assert_called_once()


@pytest.mark.asyncio
class TestRead:
    async def test_read_finds_chunk(self, adapter):
        chunk_id = "abc123"
        ref = StoredChunkRef(adapter_name="pop3", location=f"pop3://mail.example.com/{chunk_id}")

        raw_email = (
            f"From: user@example.com\r\n"
            f"X-Picard-Chunk: {chunk_id}\r\n"
            f"Content-Type: text/plain\r\n\r\n"
            f"PCHK:test:0:0:hello"
        )
        lines = [line.encode() for line in raw_email.split("\r\n")]

        mock_pop = MagicMock()
        mock_pop.stat.return_value = (1, 100)
        mock_pop.retr.return_value = (b"+OK", lines, len(raw_email))

        PopClass = MagicMock(return_value=mock_pop)

        with patch("poplib.POP3_SSL", PopClass):
            result = await adapter.read_chunk(ref)

        assert "PCHK:test:0:0:hello" in result


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_found(self, adapter):
        chunk_id = "abc123"
        ref = StoredChunkRef(adapter_name="pop3", location=f"pop3://mail.example.com/{chunk_id}")

        raw_email = f"X-Picard-Chunk: {chunk_id}\r\n\r\ndata"
        lines = [line.encode() for line in raw_email.split("\r\n")]

        mock_pop = MagicMock()
        mock_pop.stat.return_value = (1, 100)
        mock_pop.retr.return_value = (b"+OK", lines, 50)

        with patch("poplib.POP3_SSL", MagicMock(return_value=mock_pop)):
            result = await adapter.delete_chunk(ref)

        assert result is True
        mock_pop.dele.assert_called_once_with(1)


@pytest.mark.asyncio
class TestHealth:
    async def test_health_ok(self, adapter):
        mock_pop = MagicMock()
        mock_pop.stat.return_value = (0, 0)

        with patch("poplib.POP3_SSL", MagicMock(return_value=mock_pop)):
            assert await adapter.health_check() is True

    async def test_health_fail(self, adapter):
        with patch("poplib.POP3_SSL", side_effect=Exception("refused")):
            assert await adapter.health_check() is False


@pytest.mark.asyncio
class TestCapacity:
    async def test_capacity_reports_stats(self, adapter):
        mock_pop = MagicMock()
        mock_pop.stat.return_value = (42, 100000)

        with patch("poplib.POP3_SSL", MagicMock(return_value=mock_pop)):
            cap = await adapter.capacity()

        assert cap["used_bytes"] == 100000
        assert cap["chunk_count"] == 42
