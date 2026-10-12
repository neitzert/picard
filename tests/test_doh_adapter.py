"""Tests for the DNS-over-HTTPS adapter."""

from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from picard.adapters.doh_adapter import DohAdapter


@pytest.fixture
def adapter():
    return DohAdapter(
        zone="picard.example.com",
        resolver_url="https://dns.example.com/dns-query",
        nameserver="ns1.example.com",
    )


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "dns-over-https"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 100

    def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 200


@pytest.mark.asyncio
class TestWriteChunk:
    async def test_write_via_nameserver(self, adapter):
        with patch("dns.update.Update") as mock_update_cls, \
             patch("dns.query.tcp") as mock_tcp:
            mock_update = MagicMock()
            mock_update_cls.return_value = mock_update

            ref = await adapter.write_chunk("PCHK:mid:0:0:data")

        assert ref.adapter_name == "dns-over-https"
        assert "doh://" in ref.location
        mock_update.replace.assert_called_once()
        mock_tcp.assert_called_once()


@pytest.mark.asyncio
class TestReadChunk:
    async def test_read_via_doh(self, adapter):
        mock_rdata = MagicMock()
        mock_rdata.strings = [b"test-chunk-data"]
        mock_rrset = [mock_rdata]

        mock_response = MagicMock()
        mock_response.answer = [mock_rrset]

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.read = AsyncMock(return_value=b"\x00" * 50)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        from picard.adapters.base import StoredChunkRef

        ref = StoredChunkRef(
            adapter_name="dns-over-https",
            location="doh://https://dns.example.com/dns-query/test.picard.example.com",
        )

        with patch("aiohttp.ClientSession", return_value=mock_session), \
             patch("dns.message.make_query") as mock_query, \
             patch("dns.message.from_wire", return_value=mock_response):
            mock_q = MagicMock()
            mock_q.to_wire.return_value = b"\x00\x01"
            mock_query.return_value = mock_q

            result = await adapter.read_chunk(ref)

        assert result == "test-chunk-data"


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_via_nameserver(self, adapter):
        with patch("dns.update.Update") as mock_update_cls, \
             patch("dns.query.tcp") as mock_tcp:
            mock_update = MagicMock()
            mock_update_cls.return_value = mock_update

            from picard.adapters.base import StoredChunkRef

            ref = StoredChunkRef(
                adapter_name="dns-over-https",
                location="doh://resolver/c0.b0.mid.picard.example.com",
            )
            result = await adapter.delete_chunk(ref)

        assert result is True

    async def test_delete_no_nameserver(self):
        adapter = DohAdapter(
            zone="test.com",
            resolver_url="https://dns.example.com/dns-query",
        )
        from picard.adapters.base import StoredChunkRef

        ref = StoredChunkRef(
            adapter_name="dns-over-https",
            location="doh://resolver/c0.b0.mid.test.com",
        )
        result = await adapter.delete_chunk(ref)
        assert result is False


@pytest.mark.asyncio
class TestHealth:
    async def test_healthy(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session), \
             patch("dns.message.make_query") as mock_query:
            mock_q = MagicMock()
            mock_q.to_wire.return_value = b"\x00\x01"
            mock_query.return_value = mock_q

            assert await adapter.health_check() is True

    async def test_unhealthy(self, adapter):
        with patch("aiohttp.ClientSession", side_effect=Exception("fail")):
            assert await adapter.health_check() is False


@pytest.mark.asyncio
class TestListChunks:
    async def test_returns_empty(self, adapter):
        refs = await adapter.list_chunks("any")
        assert refs == []
