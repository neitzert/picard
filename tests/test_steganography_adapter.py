"""Tests for the steganography adapter — mocked I/O, real LSB logic."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.steganography import SteganographyAdapter
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


def _make_adapter(**kwargs) -> SteganographyAdapter:
    defaults = dict(image_dir="/tmp/stego")
    defaults.update(kwargs)
    return SteganographyAdapter(**defaults)


class TestLsbOperations:
    def test_embed_extract_roundtrip(self):
        pixels = bytes(8000)
        data = b"Hello, PICARD!"
        embedded = SteganographyAdapter._embed_data(pixels, data)
        extracted = SteganographyAdapter._extract_data(embedded)
        assert extracted == data

    def test_embed_extract_binary(self):
        pixels = bytes(range(256)) * 40
        data = bytes(range(256))
        embedded = SteganographyAdapter._embed_data(pixels, data)
        extracted = SteganographyAdapter._extract_data(embedded)
        assert extracted == data

    def test_embed_too_large_raises(self):
        pixels = bytes(10)
        data = b"way too large for this carrier"
        with pytest.raises(ValueError, match="too large"):
            SteganographyAdapter._embed_data(pixels, data)


@pytest.mark.asyncio
class TestSteganographyAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "steganography"
        assert p.max_chunk_bytes == 10_000
        assert p.persistent is True

    async def test_write_chunk(self):
        a = _make_adapter()
        carrier = bytes(8000)
        a._carrier_pixels = MagicMock(return_value=(carrier, 100, 100))
        a._save_image = MagicMock()

        enc = _sample_encoded()
        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "steganography"
        assert ref.location == "pchk_m1_b0_c0.png"
        a._save_image.assert_called_once()

    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        carrier = bytes(8000)
        embedded = SteganographyAdapter._embed_data(carrier, enc.encode("utf-8"))
        a._load_image = MagicMock(return_value=(embedded, 100, 100))

        ref = StoredChunkRef(adapter_name="steganography", location="pchk_m1_b0_c0.png")
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_delete_chunk(self):
        a = _make_adapter()
        mock_path = MagicMock()
        with patch.object(Path, "__truediv__", return_value=mock_path):
            mock_path.unlink = MagicMock()
            ref = StoredChunkRef(adapter_name="steganography", location="pchk_m1_b0_c0.png")
            result = await a.delete_chunk(ref)
            assert result is True

    async def test_delete_chunk_not_found(self):
        a = _make_adapter()
        mock_path = MagicMock()
        mock_path.unlink.side_effect = FileNotFoundError
        with patch.object(Path, "__truediv__", return_value=mock_path):
            ref = StoredChunkRef(adapter_name="steganography", location="nonexistent.png")
            result = await a.delete_chunk(ref)
            assert result is False

    async def test_list_chunks(self):
        a = _make_adapter()
        mock_dir = MagicMock()
        mock_dir.is_dir.return_value = True
        f1 = MagicMock()
        f1.name = "pchk_m1_b0_c0.png"
        f2 = MagicMock()
        f2.name = "pchk_m1_b0_c1.png"
        f3 = MagicMock()
        f3.name = "pchk_m2_b0_c0.png"
        mock_dir.iterdir.return_value = [f1, f2, f3]
        a._image_dir = mock_dir

        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert all(r.adapter_name == "steganography" for r in refs)

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        mock_dir = MagicMock()
        mock_dir.is_dir.return_value = True
        mock_dir.iterdir.return_value = []
        a._image_dir = mock_dir

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_list_chunks_no_dir(self):
        a = _make_adapter()
        mock_dir = MagicMock()
        mock_dir.is_dir.return_value = False
        a._image_dir = mock_dir

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        mock_dir = MagicMock()
        mock_dir.is_dir.return_value = True
        a._image_dir = mock_dir
        with patch("os.access", return_value=True):
            assert await a.health_check() is True

    async def test_health_check_no_dir(self):
        a = _make_adapter()
        mock_dir = MagicMock()
        mock_dir.is_dir.return_value = False
        a._image_dir = mock_dir
        assert await a.health_check() is False
