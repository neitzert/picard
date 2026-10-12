"""Tests for EXIF image metadata adapter."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from picard.adapters.exif_adapter import ExifAdapter


@pytest.fixture
def adapter(tmp_path):
    return ExifAdapter(tmp_path)


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "exif"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 65536

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 120


@pytest.mark.asyncio
class TestWriteRead:
    async def test_round_trip(self, adapter):
        data = "PCHK:m1:0:0:secret data in a photo"

        mock_img = MagicMock()
        mock_img.info = {}
        mock_img.save = MagicMock()
        mock_img.copy = MagicMock(return_value=mock_img)

        user_comment_bytes = b"ASCII\x00\x00\x00" + data.encode("utf-8")

        mock_piexif = MagicMock()
        mock_piexif.load.return_value = {
            "0th": {}, "Exif": {}, "GPS": {}, "1st": {},
        }
        mock_piexif.dump.return_value = b"fake-exif"

        with patch.dict("sys.modules", {"piexif": mock_piexif, "PIL": MagicMock(), "PIL.Image": MagicMock()}):
            with patch("picard.adapters.exif_adapter.ExifAdapter._create_jpeg_with_comment") as mock_create:
                ref = await adapter.write_chunk(data)

        assert ref.adapter_name == "exif"
        assert ref.location.endswith(".jpg")

        with patch.dict("sys.modules", {"piexif": mock_piexif}):
            mock_piexif.load.return_value = {
                "0th": {},
                "Exif": {0x9286: user_comment_bytes},
                "GPS": {},
                "1st": {},
            }
            with patch("picard.adapters.exif_adapter.ExifAdapter._read_comment_from_jpeg", return_value=data):
                result = await adapter.read_chunk(ref)

        assert result == data

    async def test_write_creates_file_location(self, adapter):
        with patch("picard.adapters.exif_adapter.ExifAdapter._create_jpeg_with_comment"):
            ref = await adapter.write_chunk("PCHK:m1:0:0:data")

        assert ref.location.startswith("exif://")
        assert ".jpg" in ref.location

    async def test_opaque_data(self, adapter):
        with patch("picard.adapters.exif_adapter.ExifAdapter._create_jpeg_with_comment"):
            ref = await adapter.write_chunk("sealed-opaque-data")

        assert ref.adapter_name == "exif"


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_existing(self, adapter, tmp_path):
        path = tmp_path / "test.jpg"
        path.write_bytes(b"fake-jpeg")

        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="exif", location=f"exif://{path}")

        assert await adapter.delete_chunk(ref) is True
        assert not path.exists()

    async def test_delete_nonexistent(self, adapter):
        from picard.adapters.base import StoredChunkRef

        ref = StoredChunkRef(
            adapter_name="exif", location="exif:///nonexistent.jpg"
        )
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestListChunks:
    async def test_list_by_manifest(self, adapter, tmp_path):
        (tmp_path / "manifest1_0_0.jpg").write_bytes(b"fake")
        (tmp_path / "manifest1_0_1.jpg").write_bytes(b"fake")
        (tmp_path / "other_0_0.jpg").write_bytes(b"fake")

        refs = await adapter.list_chunks("manifest1")
        assert len(refs) == 2


@pytest.mark.asyncio
class TestHealth:
    async def test_healthy(self, adapter):
        assert await adapter.health_check() is True

    async def test_unhealthy(self, tmp_path):
        bad = ExifAdapter(tmp_path / "nonexistent_sub")
        bad._dir = Path(tmp_path / "does_not_exist_at_all")
        assert await bad.health_check() is False


@pytest.mark.asyncio
class TestCapacity:
    async def test_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 0
        assert cap["used_bytes"] == 0

    async def test_with_files(self, adapter, tmp_path):
        (tmp_path / "test.jpg").write_bytes(b"x" * 100)
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 1
        assert cap["used_bytes"] == 100
