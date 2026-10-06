"""Tests for the Cloudflare DNS adapter -- mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.cloudflare_dns import (
    CloudflareDnsAdapter,
    _pack_location,
    _unpack_location,
)
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


def _make_adapter(**kwargs) -> CloudflareDnsAdapter:
    defaults = dict(
        zone_id="zone123",
        api_token="token-xxx",
        zone_name="chunks.example.com",
    )
    defaults.update(kwargs)
    return CloudflareDnsAdapter(**defaults)


def _mock_response(json_data: dict, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    return resp


class TestLocationPacking:
    def test_pack(self):
        loc = _pack_location("rec123", "c0.b0.m1.chunks.example.com")
        assert loc == "cf:rec123@c0.b0.m1.chunks.example.com"

    def test_unpack(self):
        record_id, fqdn = _unpack_location("cf:rec123@c0.b0.m1.chunks.example.com")
        assert record_id == "rec123"
        assert fqdn == "c0.b0.m1.chunks.example.com"

    def test_round_trip(self):
        loc = _pack_location("abc", "x.y.z")
        assert _unpack_location(loc) == ("abc", "x.y.z")


@pytest.mark.asyncio
class TestCloudflareDnsAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "cloudflare-dns"
        assert p.max_chunk_bytes == 200
        assert p.persistent is True
        assert p.ordered is False
        assert p.rate_limit_per_min == 1200

    async def test_fqdn(self):
        a = _make_adapter()
        assert a._fqdn("manifest1", 0, 2) == "c2.b0.manifest1.chunks.example.com"

    async def test_zone_trailing_dot_stripped(self):
        a = _make_adapter(zone_name="chunks.example.com.")
        assert a._zone_name == "chunks.example.com"

    async def test_records_url(self):
        a = _make_adapter()
        assert a._records_url() == "https://api.cloudflare.com/client/v4/zones/zone123/dns_records"
        assert a._records_url("rec1") == "https://api.cloudflare.com/client/v4/zones/zone123/dns_records/rec1"

    async def test_write_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()

        client.post.return_value = _mock_response({
            "success": True,
            "result": {"id": "rec-new-123"},
        })

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "cloudflare-dns"
        record_id, fqdn = _unpack_location(ref.location)
        assert record_id == "rec-new-123"
        assert fqdn == "c0.b0.m1.chunks.example.com"

        call_kwargs = client.post.call_args
        body = call_kwargs.kwargs["json"]
        assert body["type"] == "TXT"
        assert body["name"] == "c0.b0.m1.chunks.example.com"
        assert body["content"] == enc

    async def test_read_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()

        client.get.return_value = _mock_response({
            "success": True,
            "result": {"content": enc},
        })

        ref = StoredChunkRef(
            adapter_name="cloudflare-dns",
            location=_pack_location("rec123", "c0.b0.m1.chunks.example.com"),
        )
        result = await a.read_chunk(ref)

        assert result == enc
        client.get.assert_awaited_once_with(
            "https://api.cloudflare.com/client/v4/zones/zone123/dns_records/rec123"
        )

    async def test_delete_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.delete.return_value = _mock_response({"success": True}, status_code=200)

        ref = StoredChunkRef(
            adapter_name="cloudflare-dns",
            location=_pack_location("rec123", "c0.b0.m1.chunks.example.com"),
        )
        assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_not_found(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.delete.return_value = _mock_response({"success": False}, status_code=404)

        ref = StoredChunkRef(
            adapter_name="cloudflare-dns",
            location=_pack_location("rec123", "c0.b0.m1.chunks.example.com"),
        )
        assert await a.delete_chunk(ref) is False

    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.delete.side_effect = Exception("connection lost")

        ref = StoredChunkRef(
            adapter_name="cloudflare-dns",
            location=_pack_location("rec123", "c0.b0.m1.chunks.example.com"),
        )
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response({
            "success": True,
            "result": [
                {"id": "r1", "name": "c0.b0.m1.chunks.example.com"},
                {"id": "r2", "name": "c1.b0.m1.chunks.example.com"},
                {"id": "r3", "name": "c0.b0.m2.chunks.example.com"},
            ],
            "result_info": {"total_pages": 1, "page": 1},
        })

        refs = await a.list_chunks("m1")

        assert len(refs) == 2
        assert all(r.adapter_name == "cloudflare-dns" for r in refs)
        ids = [_unpack_location(r.location)[0] for r in refs]
        assert "r1" in ids
        assert "r2" in ids

    async def test_list_chunks_pagination(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        page1 = _mock_response({
            "success": True,
            "result": [
                {"id": "r1", "name": "c0.b0.m1.chunks.example.com"},
            ],
            "result_info": {"total_pages": 2, "page": 1},
        })
        page2 = _mock_response({
            "success": True,
            "result": [
                {"id": "r2", "name": "c1.b0.m1.chunks.example.com"},
            ],
            "result_info": {"total_pages": 2, "page": 2},
        })
        client.get.side_effect = [page1, page2]

        refs = await a.list_chunks("m1")

        assert len(refs) == 2
        assert client.get.await_count == 2

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response({
            "success": True,
            "result": [],
            "result_info": {"total_pages": 1, "page": 1},
        })

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response({"success": True}, status_code=200)
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response({"success": False}, status_code=403)
        assert await a.health_check() is False

    async def test_health_check_exception(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.get.side_effect = Exception("timeout")
        assert await a.health_check() is False

    async def test_custom_base_url(self):
        a = _make_adapter(base_url="https://custom.api.dev/v4/")
        assert a._records_url() == "https://custom.api.dev/v4/zones/zone123/dns_records"
