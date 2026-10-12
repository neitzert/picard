"""Tests for the Windows Registry adapter."""

import sys
from unittest.mock import MagicMock, patch

import pytest

fake_winreg = MagicMock()
fake_winreg.HKEY_CURRENT_USER = 0x80000001
fake_winreg.HKEY_LOCAL_MACHINE = 0x80000002
fake_winreg.HKEY_USERS = 0x80000003
fake_winreg.HKEY_CLASSES_ROOT = 0x80000000
fake_winreg.HKEY_CURRENT_CONFIG = 0x80000005
fake_winreg.KEY_WRITE = 0x20006
fake_winreg.KEY_READ = 0x20019
fake_winreg.KEY_SET_VALUE = 0x0002
fake_winreg.REG_SZ = 1
sys.modules["winreg"] = fake_winreg

from picard.adapters.windows_registry import WindowsRegistryAdapter
from picard.adapters.base import StoredChunkRef


@pytest.fixture(autouse=True)
def reset_mocks():
    fake_winreg.reset_mock()
    fake_winreg.CreateKeyEx.side_effect = None
    fake_winreg.OpenKeyEx.side_effect = None
    fake_winreg.DeleteValue.side_effect = None
    fake_winreg.EnumValue.side_effect = None
    fake_winreg.QueryValueEx.side_effect = None
    yield


@pytest.fixture
def adapter():
    return WindowsRegistryAdapter(hive="HKCU", base_key=r"Software\PICARD\Test")


@pytest.mark.asyncio
class TestProfile:
    async def test_name(self, adapter):
        assert adapter.profile().name == "windows-registry"

    async def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    async def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 1_048_576

    async def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 1000


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter):
        mock_key = MagicMock()
        fake_winreg.CreateKeyEx.return_value = mock_key
        ref = await adapter.write_chunk("test-data")
        assert ref.adapter_name == "windows-registry"
        assert "winreg://" in ref.location

    async def test_read_returns_data(self, adapter):
        mock_key = MagicMock()
        fake_winreg.OpenKeyEx.return_value = mock_key
        fake_winreg.QueryValueEx.return_value = ("test-data", 1)
        ref = StoredChunkRef(adapter_name="windows-registry", location="winreg://HKCU/Software\\PICARD\\Test/my_key")
        result = await adapter.read_chunk(ref)
        assert result == "test-data"

    async def test_delete_existing(self, adapter):
        mock_key = MagicMock()
        fake_winreg.OpenKeyEx.return_value = mock_key
        ref = StoredChunkRef(adapter_name="windows-registry", location="winreg://HKCU/key/val")
        result = await adapter.delete_chunk(ref)
        assert result is True

    async def test_delete_missing(self, adapter):
        mock_key = MagicMock()
        fake_winreg.OpenKeyEx.return_value = mock_key
        fake_winreg.DeleteValue.side_effect = FileNotFoundError
        ref = StoredChunkRef(adapter_name="windows-registry", location="winreg://HKCU/key/val")
        result = await adapter.delete_chunk(ref)
        assert result is False


@pytest.mark.asyncio
class TestListChunks:
    async def test_list_empty(self, adapter):
        fake_winreg.OpenKeyEx.side_effect = FileNotFoundError
        refs = await adapter.list_chunks("manifest123")
        assert refs == []

    async def test_list_with_matching(self, adapter):
        mock_key = MagicMock()
        fake_winreg.OpenKeyEx.return_value = mock_key
        call_count = [0]

        def enum_side_effect(key, i):
            if call_count[0] == 0:
                call_count[0] += 1
                return ("manifest123_0_0", "data", 1)
            raise OSError

        fake_winreg.EnumValue.side_effect = enum_side_effect
        refs = await adapter.list_chunks("manifest123")
        assert len(refs) == 1


@pytest.mark.asyncio
class TestHealth:
    async def test_health_ok(self, adapter):
        mock_key = MagicMock()
        fake_winreg.CreateKeyEx.return_value = mock_key
        assert await adapter.health_check() is True

    async def test_health_fail(self, adapter):
        fake_winreg.CreateKeyEx.side_effect = Exception("denied")
        assert await adapter.health_check() is False


@pytest.mark.asyncio
class TestCapacity:
    async def test_capacity_returns_nones(self, adapter):
        cap = await adapter.capacity()
        assert cap["used_bytes"] is None
        assert cap["chunk_count"] is None
