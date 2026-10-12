"""Tests for the DNS adapter -- mocked, no network calls."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.dns import DnsAdapter
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


def _make_adapter(**kwargs) -> DnsAdapter:
    defaults = dict(zone="chunks.example.com", nameserver="127.0.0.1")
    defaults.update(kwargs)
    return DnsAdapter(**defaults)


@pytest.fixture(autouse=True)
def _fake_dns_modules(monkeypatch):
    """Inject fake dns.* modules so the adapter's lazy imports resolve."""
    dns_mod = MagicMock()
    monkeypatch.setitem(sys.modules, "dns", dns_mod)
    monkeypatch.setitem(sys.modules, "dns.query", dns_mod.query)
    monkeypatch.setitem(sys.modules, "dns.update", dns_mod.update)
    monkeypatch.setitem(sys.modules, "dns.resolver", dns_mod.resolver)
    monkeypatch.setitem(sys.modules, "dns.zone", dns_mod.zone)
    monkeypatch.setitem(sys.modules, "dns.tsigkeyring", dns_mod.tsigkeyring)
    return dns_mod


@pytest.mark.asyncio
class TestDnsAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "dns"
        assert p.max_chunk_bytes == 200
        assert p.persistent is True
        assert p.ordered is False

    async def test_fqdn(self):
        a = _make_adapter()
        fqdn = a._fqdn("manifest1", 0, 2)
        assert fqdn == "c2.b0.manifest1.chunks.example.com"

    async def test_write_chunk(self, _fake_dns_modules):
        a = _make_adapter()
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "dns"
        assert "c0.b0.m1.chunks.example.com" in ref.location
        _fake_dns_modules.query.tcp.assert_called_once()

    async def test_write_chunk_with_tsig(self, _fake_dns_modules):
        a = _make_adapter(
            tsig_keyname="picard-key",
            tsig_key="c2VjcmV0",
            tsig_algorithm="hmac-sha256",
        )
        enc = _sample_encoded()

        mock_update = MagicMock()
        _fake_dns_modules.update.Update.return_value = mock_update
        _fake_dns_modules.tsigkeyring.from_text.return_value = {"picard-key": b"secret"}

        await a.write_chunk(enc)

        _fake_dns_modules.tsigkeyring.from_text.assert_called_once_with(
            {"picard-key": "c2VjcmV0"}
        )
        mock_update.use_tsig.assert_called_once()

    async def test_read_chunk(self, _fake_dns_modules):
        a = _make_adapter()
        enc = _sample_encoded()
        ref = StoredChunkRef(
            adapter_name="dns",
            location="c0.b0.m1.chunks.example.com",
        )

        mock_rdata = MagicMock()
        mock_rdata.strings = [enc.encode("utf-8")]
        _fake_dns_modules.resolver.resolve.return_value = [mock_rdata]

        result = await a.read_chunk(ref)

        _fake_dns_modules.resolver.resolve.assert_called_once_with(
            "c0.b0.m1.chunks.example.com", "TXT"
        )
        assert result == enc

    async def test_delete_chunk(self, _fake_dns_modules):
        a = _make_adapter()
        ref = StoredChunkRef(
            adapter_name="dns",
            location="c0.b0.m1.chunks.example.com",
        )

        mock_update = MagicMock()
        _fake_dns_modules.update.Update.return_value = mock_update

        assert await a.delete_chunk(ref) is True
        mock_update.delete.assert_called_once()

    async def test_delete_chunk_failure(self, _fake_dns_modules):
        a = _make_adapter()
        ref = StoredChunkRef(
            adapter_name="dns",
            location="c0.b0.m1.chunks.example.com",
        )

        _fake_dns_modules.query.tcp.side_effect = Exception("refused")

        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self, _fake_dns_modules):
        a = _make_adapter()

        mock_zone = MagicMock()
        mock_zone.nodes = {
            "c0.b0.m1": MagicMock(),
            "c1.b0.m1": MagicMock(),
            "c0.b0.m2": MagicMock(),
        }
        _fake_dns_modules.zone.from_xfr.return_value = mock_zone

        refs = await a.list_chunks("m1")

        matching = [r for r in refs if ".m1." in r.location]
        assert len(matching) >= 2
        assert all(r.adapter_name == "dns" for r in refs)

    async def test_list_chunks_axfr_refused(self, _fake_dns_modules):
        a = _make_adapter()

        _fake_dns_modules.zone.from_xfr.side_effect = Exception("refused")

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self, _fake_dns_modules):
        a = _make_adapter()
        _fake_dns_modules.resolver.resolve.return_value = [MagicMock()]
        assert await a.health_check() is True

    async def test_health_check_fail(self, _fake_dns_modules):
        a = _make_adapter()
        _fake_dns_modules.resolver.resolve.side_effect = Exception("NXDOMAIN")
        assert await a.health_check() is False

    async def test_zone_trailing_dot_stripped(self):
        a = _make_adapter(zone="chunks.example.com.")
        assert a._zone == "chunks.example.com"
