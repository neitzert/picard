"""Tests for the barcode adapter."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

# Pre-inject mock modules before adapter import
_mock_pylibdmtx = MagicMock()
_mock_pyzbar = MagicMock()
_mock_pil = sys.modules.get("PIL", MagicMock())
sys.modules.setdefault("pylibdmtx", MagicMock())
sys.modules.setdefault("pylibdmtx.pylibdmtx", _mock_pylibdmtx)
sys.modules.setdefault("pyzbar", MagicMock())
sys.modules.setdefault("pyzbar.pyzbar", _mock_pyzbar)

from picard.adapters.barcode_adapter import BarcodeAdapter


@pytest.fixture
def adapter(tmp_path):
    return BarcodeAdapter(output_dir=tmp_path)


@pytest.mark.asyncio
class TestProfile:
    async def test_name(self, adapter):
        assert adapter.profile().name == "barcode"

    async def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    async def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 2335


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_creates_file(self, adapter):
        mock_encoded = MagicMock()
        mock_encoded.width = 10
        mock_encoded.height = 10
        mock_encoded.pixels = b"\x00" * 300
        _mock_pylibdmtx.encode.return_value = mock_encoded
        mock_img = MagicMock()
        _mock_pil.Image.frombytes.return_value = mock_img

        ref = await adapter.write_chunk("barcode-data")
        assert ref.adapter_name == "barcode"
        assert ref.location.startswith("barcode://")

    async def test_read(self, adapter, tmp_path):
        from unittest.mock import patch

        test_file = tmp_path / "test.png"
        test_file.write_bytes(b"fake-png")

        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="barcode", location=f"barcode://{test_file}")

        mock_result = MagicMock()
        mock_result.data = b"decoded-data"
        _mock_pyzbar.decode.return_value = [mock_result]

        with patch("PIL.Image.open", return_value=MagicMock()):
            result = await adapter.read_chunk(ref)
        assert result == "decoded-data"

    async def test_read_no_barcode(self, adapter, tmp_path):
        from unittest.mock import patch

        test_file = tmp_path / "empty.png"
        test_file.write_bytes(b"fake")

        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="barcode", location=f"barcode://{test_file}")
        _mock_pyzbar.decode.return_value = []

        with patch("PIL.Image.open", return_value=MagicMock()):
            with pytest.raises(ValueError, match="no barcode"):
                await adapter.read_chunk(ref)


@pytest.mark.asyncio
class TestDelete:
    async def test_delete(self, adapter, tmp_path):
        test_file = tmp_path / "del.png"
        test_file.write_bytes(b"fake")

        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="barcode", location=f"barcode://{test_file}")
        assert await adapter.delete_chunk(ref) is True
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestList:
    async def test_list_chunks(self, adapter, tmp_path):
        (tmp_path / "man1_0_0.png").write_bytes(b"fake")
        (tmp_path / "man1_0_1.png").write_bytes(b"fake")
        refs = await adapter.list_chunks("man1")
        assert len(refs) == 2


@pytest.mark.asyncio
class TestCapacity:
    async def test_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 0
        assert cap["free_bytes"] is not None

    async def test_after_files(self, adapter, tmp_path):
        (tmp_path / "chunk.png").write_bytes(b"x" * 100)
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 1
        assert cap["used_bytes"] == 100


@pytest.mark.asyncio
class TestHealthCheck:
    async def test_healthy(self, adapter):
        assert await adapter.health_check() is True
