"""Tests for the Kerberos adapter -- mocked, no real KDC."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.kerberos import KerberosAdapter
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


def _make_adapter(tmp_path: Path | None = None, **kwargs) -> KerberosAdapter:
    defaults = dict(
        realm="EXAMPLE.COM",
        principal="picard@EXAMPLE.COM",
        store_dir=tmp_path or Path("/tmp/picard_krb_test"),
    )
    defaults.update(kwargs)
    return KerberosAdapter(**defaults)


@pytest.mark.asyncio
class TestKerberosProfile:
    async def test_defaults(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "kerberos"
        assert p.max_chunk_bytes == 4096
        assert p.persistent is False
        assert p.rate_limit_per_min == 30
        assert p.ttl_seconds == 36000

    async def test_custom_max(self):
        a = _make_adapter(max_chunk_bytes=2048)
        assert a.profile().max_chunk_bytes == 2048


@pytest.mark.asyncio
class TestKerberosWrite:
    @patch("picard.adapters.kerberos.KerberosAdapter._init_gssapi")
    async def test_write_chunk(self, mock_gssapi, tmp_path):
        mock_gssapi.return_value = MagicMock()

        a = _make_adapter(tmp_path=tmp_path)
        enc = _sample_encoded()
        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "kerberos"
        assert "krb5://EXAMPLE.COM/" in ref.location
        assert "m1" in ref.location
        stored = list(tmp_path.glob("*.pchk"))
        assert len(stored) == 1
        assert stored[0].read_text(encoding="utf-8") == enc

    @patch("picard.adapters.kerberos.KerberosAdapter._init_gssapi")
    async def test_write_opaque_data(self, mock_gssapi, tmp_path):
        mock_gssapi.return_value = MagicMock()

        a = _make_adapter(tmp_path=tmp_path)
        ref = await a.write_chunk("opaque-sealed-data")

        assert ref.adapter_name == "kerberos"
        stored = list(tmp_path.glob("*.pchk"))
        assert len(stored) == 1


@pytest.mark.asyncio
class TestKerberosRead:
    @patch("picard.adapters.kerberos.KerberosAdapter._init_gssapi")
    async def test_round_trip(self, mock_gssapi, tmp_path):
        mock_gssapi.return_value = MagicMock()

        a = _make_adapter(tmp_path=tmp_path)
        enc = _sample_encoded()
        ref = await a.write_chunk(enc)
        result = await a.read_chunk(ref)

        assert result == enc

    @patch("picard.adapters.kerberos.KerberosAdapter._init_gssapi")
    async def test_read_not_found(self, mock_gssapi, tmp_path):
        mock_gssapi.return_value = MagicMock()

        a = _make_adapter(tmp_path=tmp_path)
        ref = StoredChunkRef(adapter_name="kerberos", location="krb5://EXAMPLE.COM/missing")
        with pytest.raises(KeyError, match="chunk not found"):
            await a.read_chunk(ref)


@pytest.mark.asyncio
class TestKerberosDelete:
    @patch("picard.adapters.kerberos.KerberosAdapter._init_gssapi")
    async def test_delete_success(self, mock_gssapi, tmp_path):
        mock_gssapi.return_value = MagicMock()

        a = _make_adapter(tmp_path=tmp_path)
        enc = _sample_encoded()
        ref = await a.write_chunk(enc)
        assert await a.delete_chunk(ref) is True
        assert not list(tmp_path.glob("*.pchk"))

    @patch("picard.adapters.kerberos.KerberosAdapter._init_gssapi")
    async def test_delete_not_found(self, mock_gssapi, tmp_path):
        mock_gssapi.return_value = MagicMock()

        a = _make_adapter(tmp_path=tmp_path)
        ref = StoredChunkRef(adapter_name="kerberos", location="krb5://EXAMPLE.COM/missing")
        assert await a.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestKerberosList:
    @patch("picard.adapters.kerberos.KerberosAdapter._init_gssapi")
    async def test_list_chunks(self, mock_gssapi, tmp_path):
        mock_gssapi.return_value = MagicMock()

        a = _make_adapter(tmp_path=tmp_path)
        await a.write_chunk(_sample_encoded("m1", 0, 0))
        await a.write_chunk(_sample_encoded("m1", 0, 1))

        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert all(r.adapter_name == "kerberos" for r in refs)


@pytest.mark.asyncio
class TestKerberosHealth:
    @patch("picard.adapters.kerberos.KerberosAdapter._init_gssapi")
    async def test_health_ok(self, mock_gssapi):
        mock_gssapi.return_value = MagicMock()

        a = _make_adapter()
        assert await a.health_check() is True

    @patch("picard.adapters.kerberos.KerberosAdapter._init_gssapi")
    async def test_health_fail(self, mock_gssapi):
        mock_gssapi.side_effect = Exception("no TGT")

        a = _make_adapter()
        assert await a.health_check() is False


@pytest.mark.asyncio
class TestKerberosCapacity:
    @patch("picard.adapters.kerberos.KerberosAdapter._init_gssapi")
    async def test_capacity(self, mock_gssapi, tmp_path):
        mock_gssapi.return_value = MagicMock()

        a = _make_adapter(tmp_path=tmp_path)
        await a.write_chunk(_sample_encoded())
        cap = await a.capacity()
        assert cap["chunk_count"] == 1
        assert cap["used_bytes"] > 0
        assert cap["free_bytes"] is None
