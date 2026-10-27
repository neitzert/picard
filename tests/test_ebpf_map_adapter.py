"""Unit tests for ebpf_map_adapter (Linux-only, syscall mocked)."""
import sys
import pytest

if sys.platform != "linux":
    pytest.skip("ebpf_map_adapter requires Linux", allow_module_level=True)

import asyncio
from unittest.mock import patch, MagicMock
from picard.adapters.base import StoredChunkRef

CHUNK = "hello"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_adapter():
    with patch("picard.adapters.ebpf_map_adapter._create_map", return_value=5), \
         patch("picard.adapters.ebpf_map_adapter._pin_map"), \
         patch("os.makedirs"), \
         patch("os.path.exists", return_value=False):
        from picard.adapters.ebpf_map_adapter import EbpfMapAdapter
        a = EbpfMapAdapter()
        a._fd = 5
    return a


def test_profile():
    a = _make_adapter()
    p = a.profile()
    assert p.name == "ebpf_map"


def test_write_read_delete():
    a = _make_adapter()
    store = {}

    def _mock_update(fd, key_b, val_b):
        store[key_b] = val_b

    def _mock_lookup(fd, key_b):
        if key_b not in store:
            raise KeyError("not found")
        return store[key_b]

    def _mock_delete(fd, key_b):
        if key_b in store:
            del store[key_b]
            return True
        return False

    def _mock_next(fd, prev):
        keys = list(store.keys())
        if prev is None:
            return keys[0] if keys else None
        try:
            idx = keys.index(prev)
            return keys[idx + 1] if idx + 1 < len(keys) else None
        except (ValueError, IndexError):
            return None

    with patch("picard.adapters.ebpf_map_adapter._map_update", side_effect=_mock_update), \
         patch("picard.adapters.ebpf_map_adapter._map_lookup", side_effect=_mock_lookup), \
         patch("picard.adapters.ebpf_map_adapter._map_delete", side_effect=_mock_delete), \
         patch("picard.adapters.ebpf_map_adapter._map_next_key", side_effect=_mock_next):

        ref = _run(a.write_chunk(CHUNK))
        assert ref.adapter_name == "ebpf_map"
        assert ref.location.startswith("ebpf://")

        data = _run(a.read_chunk(ref))
        assert data == CHUNK

        deleted = _run(a.delete_chunk(ref))
        assert deleted is True


def test_health_check():
    a = _make_adapter()
    with patch("os.path.isdir", return_value=True):
        result = _run(a.health_check())
    assert result is True


def test_health_check_no_bpffs():
    a = _make_adapter()
    with patch("os.path.isdir", return_value=False):
        result = _run(a.health_check())
    assert result is False
