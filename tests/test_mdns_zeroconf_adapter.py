"""Unit tests for mdns_zeroconf_adapter (zeroconf import mocked)."""
import asyncio
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from picard.adapters.mdns_zeroconf_adapter import MdnsZeroconfAdapter
from picard.adapters.base import StoredChunkRef

CHUNK = "hello"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _mock_zeroconf_modules():
    mock_zc = MagicMock()
    mock_info = MagicMock()
    mock_service_info = MagicMock(return_value=mock_info)
    mock_zeroconf_mod = MagicMock()
    mock_zeroconf_mod.Zeroconf = MagicMock(return_value=mock_zc)
    mock_zeroconf_mod.ServiceInfo = mock_service_info
    return mock_zeroconf_mod, mock_zc, mock_info


def test_profile():
    a = MdnsZeroconfAdapter()
    p = a.profile()
    assert p.name == "mdns_zeroconf"


def test_health_check_no_zeroconf():
    a = MdnsZeroconfAdapter()
    with patch.dict("sys.modules", {"zeroconf": None}):
        result = _run(a.health_check())
    assert result is False


def test_health_check_with_zeroconf():
    a = MdnsZeroconfAdapter()
    mock_mod, mock_zc, _ = _mock_zeroconf_modules()
    with patch.dict("sys.modules", {"zeroconf": mock_mod}):
        result = _run(a.health_check())
    assert result is True


def test_write_read_delete():
    a = MdnsZeroconfAdapter()
    mock_mod, mock_zc, mock_info = _mock_zeroconf_modules()

    with patch.dict("sys.modules", {"zeroconf": mock_mod}), \
         patch("asyncio.to_thread", new=AsyncMock(return_value=None)):
        ref = _run(a.write_chunk(CHUNK))

    assert ref.adapter_name == "mdns_zeroconf"
    assert ref.location.startswith("mdns://")

    # read from in-process registry (no network call needed)
    data = _run(a.read_chunk(ref))
    assert data == CHUNK

    with patch.dict("sys.modules", {"zeroconf": mock_mod}), \
         patch("asyncio.to_thread", new=AsyncMock(return_value=None)):
        deleted = _run(a.delete_chunk(ref))
    assert deleted is True

    with pytest.raises(KeyError):
        _run(a.read_chunk(ref))


def test_list_chunks():
    a = MdnsZeroconfAdapter()
    mock_mod, _, _ = _mock_zeroconf_modules()
    pchk = "PCHK:1:mdnsmid:0:0:1:YQ=="
    with patch.dict("sys.modules", {"zeroconf": mock_mod}), \
         patch("asyncio.to_thread", new=AsyncMock(return_value=None)):
        _run(a.write_chunk(pchk))
    refs = _run(a.list_chunks("mdnsmid"))
    assert len(refs) == 1
