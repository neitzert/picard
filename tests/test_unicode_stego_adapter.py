"""Tests for the Unicode steganography adapter."""

import pytest

from picard.adapters.unicode_stego import UnicodeStegoAdapter, _encode_zwc, _decode_zwc
from picard.adapters.base import StoredChunkRef


@pytest.fixture
def adapter(tmp_path):
    return UnicodeStegoAdapter(output_dir=tmp_path / "stego", max_chunk_bytes=4096)


@pytest.mark.asyncio
class TestProfile:
    async def test_name(self, adapter):
        assert adapter.profile().name == "unicode-stego"

    async def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    async def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 4096


class TestEncoding:
    def test_round_trip_bytes(self):
        data = b"Hello, World!"
        encoded = _encode_zwc(data)
        decoded = _decode_zwc(encoded)
        assert decoded == data

    def test_round_trip_binary(self):
        data = bytes(range(256))
        encoded = _encode_zwc(data)
        decoded = _decode_zwc(encoded)
        assert decoded == data

    def test_invisible_chars(self):
        data = b"secret"
        encoded = _encode_zwc(data)
        visible = encoded.replace("​", "").replace("‌", "").replace("‍", "").replace("﻿", "")
        assert visible == ""

    def test_interleaved_with_text(self):
        data = b"hi"
        encoded = _encode_zwc(data)
        mixed = "normal text" + encoded + " more text"
        decoded = _decode_zwc(mixed)
        assert decoded == data


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter):
        ref = await adapter.write_chunk("stego-data")
        assert ref.adapter_name == "unicode-stego"
        assert "zwc://" in ref.location

    async def test_round_trip(self, adapter):
        data = "round-trip-stego"
        ref = await adapter.write_chunk(data)
        result = await adapter.read_chunk(ref)
        assert result == data

    async def test_file_looks_normal(self, adapter, tmp_path):
        ref = await adapter.write_chunk("hidden-secret")
        filename = ref.location.split("://", 1)[1]
        from pathlib import Path
        content = Path(filename).read_text(encoding="utf-8")
        visible = content.replace("​", "").replace("‌", "").replace("‍", "").replace("﻿", "")
        assert "hidden-secret" not in visible
        assert "fox" in visible or "dog" in visible


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_existing(self, adapter):
        ref = await adapter.write_chunk("delete-me")
        assert await adapter.delete_chunk(ref) is True

    async def test_delete_missing(self, adapter, tmp_path):
        ref = StoredChunkRef(adapter_name="unicode-stego", location=f"zwc://{tmp_path}/nope.txt")
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
        await adapter.write_chunk("some data")
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 1
        assert cap["used_bytes"] > 0
