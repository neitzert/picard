"""Tests for the CSS steganography adapter."""

import pytest

from picard.adapters.css_stego import CssStegoAdapter
from picard.adapters.base import StoredChunkRef


@pytest.fixture
def adapter(tmp_path):
    return CssStegoAdapter(output_dir=tmp_path / "css_store", max_chunk_bytes=4096)


@pytest.mark.asyncio
class TestProfile:
    async def test_name(self, adapter):
        assert adapter.profile().name == "css-stego"

    async def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    async def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 4096


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter):
        ref = await adapter.write_chunk("css-data")
        assert ref.adapter_name == "css-stego"
        assert "css://" in ref.location

    async def test_round_trip(self, adapter):
        data = "round-trip-css-secret"
        ref = await adapter.write_chunk(data)
        result = await adapter.read_chunk(ref)
        assert result == data

    async def test_css_looks_normal(self, adapter):
        ref = await adapter.write_chunk("hidden-in-css")
        filename = ref.location.split("://", 1)[1]
        from pathlib import Path
        content = Path(filename).read_text(encoding="utf-8")
        assert ":root" in content
        assert "body" in content
        assert "--font-size" in content
        assert "--picard-data" in content

    async def test_data_in_custom_property(self, adapter):
        ref = await adapter.write_chunk("custom-prop-data")
        filename = ref.location.split("://", 1)[1]
        from pathlib import Path
        content = Path(filename).read_text(encoding="utf-8")
        assert '--picard-data: "' in content


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_existing(self, adapter):
        ref = await adapter.write_chunk("delete-me")
        assert await adapter.delete_chunk(ref) is True

    async def test_delete_missing(self, adapter, tmp_path):
        ref = StoredChunkRef(adapter_name="css-stego", location=f"css://{tmp_path}/nope.css")
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestListChunks:
    async def test_list_empty(self, adapter):
        refs = await adapter.list_chunks("nonexistent")
        assert refs == []


@pytest.mark.asyncio
class TestHealth:
    async def test_health_ok(self, adapter):
        assert await adapter.health_check() is True


@pytest.mark.asyncio
class TestCapacity:
    async def test_capacity_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["chunk_count"] is None or cap["chunk_count"] == 0

    async def test_capacity_with_data(self, adapter):
        await adapter.write_chunk("data")
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 1
        assert cap["used_bytes"] > 0
