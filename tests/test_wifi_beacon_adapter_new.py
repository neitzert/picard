"""Unit tests for wifi_beacon_adapter."""
import asyncio
import pytest
from picard.adapters.wifi_beacon_adapter import WifiBeaconAdapter, _build_vendor_ie
from picard.adapters.base import StoredChunkRef

CHUNK = "hello"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_profile():
    a = WifiBeaconAdapter()
    p = a.profile()
    assert p.name == "wifi_beacon"
    assert p.max_chunk_bytes == 220


def test_build_vendor_ie_roundtrip():
    payload = "hello".encode().hex()
    ie = _build_vendor_ie(payload)
    assert ie.startswith("dd")
    # header = "dd" + 2-char len + 6-char OUI + 2-char type = 12 chars
    header_len = 12
    recovered = bytes.fromhex(ie[header_len:]).decode()
    assert recovered == "hello"


def test_write_read_delete():
    a = WifiBeaconAdapter()
    ref = _run(a.write_chunk(CHUNK))
    assert ref.adapter_name == "wifi_beacon"

    data = _run(a.read_chunk(ref))
    assert data == CHUNK

    deleted = _run(a.delete_chunk(ref))
    assert deleted is True

    with pytest.raises(KeyError):
        _run(a.read_chunk(ref))


def test_chunk_too_large():
    a = WifiBeaconAdapter(max_chunk_bytes=4)
    with pytest.raises(ValueError):
        _run(a.write_chunk("toolong"))


def test_list_chunks():
    a = WifiBeaconAdapter()
    _run(a.write_chunk(CHUNK))
    refs = _run(a.list_chunks(""))
    assert len(refs) == 1


def test_generate_hostapd_conf():
    a = WifiBeaconAdapter(interface="wlan1", ssid="TEST", channel=11)
    ref = _run(a.write_chunk(CHUNK))
    conf = a.generate_hostapd_conf(ref)
    assert "wlan1" in conf
    assert "TEST" in conf
    assert "vendor_elements=" in conf
