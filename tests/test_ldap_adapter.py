"""Tests for the LDAP adapter -- mocked, no real LDAP server."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.ldap_adapter import LdapAdapter
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


def _make_adapter(**kwargs) -> LdapAdapter:
    defaults = dict(
        host="ldap.example.com",
        base_dn="dc=example,dc=com",
        bind_dn="cn=admin,dc=example,dc=com",
        bind_password="secret",
    )
    defaults.update(kwargs)
    return LdapAdapter(**defaults)


@pytest.mark.asyncio
class TestLdapProfile:
    async def test_defaults(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "ldap"
        assert p.max_chunk_bytes == 63_000
        assert p.persistent is True
        assert p.rate_limit_per_min == 600

    async def test_custom_max(self):
        a = _make_adapter(max_chunk_bytes=1000)
        assert a.profile().max_chunk_bytes == 1000


@pytest.mark.asyncio
class TestLdapWrite:
    @patch("picard.adapters.ldap_adapter.LdapAdapter._connect")
    async def test_write_chunk(self, mock_connect):
        conn = MagicMock()
        mock_connect.return_value = conn

        a = _make_adapter()
        enc = _sample_encoded()
        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "ldap"
        assert "ldap://ldap.example.com/" in ref.location
        assert "m1" in ref.location
        conn.add.assert_called_once()
        conn.unbind.assert_called_once()

    @patch("picard.adapters.ldap_adapter.LdapAdapter._connect")
    async def test_write_opaque_data(self, mock_connect):
        conn = MagicMock()
        mock_connect.return_value = conn

        a = _make_adapter()
        ref = await a.write_chunk("opaque-sealed-data")

        assert ref.adapter_name == "ldap"
        assert "ldap://ldap.example.com/" in ref.location
        conn.add.assert_called_once()


@pytest.mark.asyncio
class TestLdapRead:
    @patch("picard.adapters.ldap_adapter.LdapAdapter._connect")
    async def test_read_chunk(self, mock_connect):
        enc = _sample_encoded()
        entry = MagicMock()
        entry.description = enc

        conn = MagicMock()
        conn.entries = [entry]
        mock_connect.return_value = conn

        a = _make_adapter()
        ref = StoredChunkRef(
            adapter_name="ldap",
            location="ldap://ldap.example.com/ou=c0.b0.m1,dc=example,dc=com",
        )
        result = await a.read_chunk(ref)

        assert result == enc
        conn.search.assert_called_once()
        conn.unbind.assert_called_once()

    @patch("picard.adapters.ldap_adapter.LdapAdapter._connect")
    async def test_read_not_found(self, mock_connect):
        conn = MagicMock()
        conn.entries = []
        mock_connect.return_value = conn

        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="ldap", location="ldap://ldap.example.com/ou=missing,dc=example,dc=com")
        with pytest.raises(KeyError, match="chunk not found"):
            await a.read_chunk(ref)


@pytest.mark.asyncio
class TestLdapDelete:
    @patch("picard.adapters.ldap_adapter.LdapAdapter._connect")
    async def test_delete_success(self, mock_connect):
        conn = MagicMock()
        conn.result = {"result": 0}
        mock_connect.return_value = conn

        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="ldap", location="ldap://ldap.example.com/ou=c0.b0.m1,dc=example,dc=com")
        assert await a.delete_chunk(ref) is True
        conn.delete.assert_called_once()

    @patch("picard.adapters.ldap_adapter.LdapAdapter._connect")
    async def test_delete_failure(self, mock_connect):
        conn = MagicMock()
        conn.delete.side_effect = Exception("no such entry")
        mock_connect.return_value = conn

        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="ldap", location="ldap://ldap.example.com/ou=missing,dc=example,dc=com")
        assert await a.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestLdapList:
    @patch("picard.adapters.ldap_adapter.LdapAdapter._connect")
    async def test_list_chunks(self, mock_connect):
        entry1 = MagicMock()
        entry1.entry_dn = "ou=c0.b0.m1,dc=example,dc=com"
        entry2 = MagicMock()
        entry2.entry_dn = "ou=c1.b0.m1,dc=example,dc=com"

        conn = MagicMock()
        conn.entries = [entry1, entry2]
        mock_connect.return_value = conn

        a = _make_adapter()
        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert all(r.adapter_name == "ldap" for r in refs)


@pytest.mark.asyncio
class TestLdapHealth:
    @patch("picard.adapters.ldap_adapter.LdapAdapter._connect")
    async def test_health_ok(self, mock_connect):
        conn = MagicMock()
        mock_connect.return_value = conn

        a = _make_adapter()
        assert await a.health_check() is True

    @patch("picard.adapters.ldap_adapter.LdapAdapter._connect")
    async def test_health_fail(self, mock_connect):
        mock_connect.side_effect = Exception("connection refused")

        a = _make_adapter()
        assert await a.health_check() is False
