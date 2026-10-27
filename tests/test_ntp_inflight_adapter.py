"""Unit tests for ntp_inflight_adapter."""
import asyncio
import pytest
from unittest.mock import patch
from picard.adapters.ntp_inflight_adapter import NtpInflightAdapter
from picard.adapters.base import StoredChunkRef

CHUNK = "hi"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _adapter():
    return NtpInflightAdapter(max_chunk_bytes=16)


def test_profile():
    a = _adapter()
    p = a.profile()
    assert p.name == "ntp-inflight"


def test_health_check():
    a = _adapter()
    result = _run(a.health_check())
    assert result is True


def test_write_read_delete():
    a = _adapter()
    with patch.object(a, "_send_udp", return_value=None):
        ref = _run(a.write_chunk(CHUNK))
    assert ref.adapter_name == "ntp-inflight"
    assert ref.location.startswith("ntp://")

    data = _run(a.read_chunk(ref))
    assert data == CHUNK

    deleted = _run(a.delete_chunk(ref))
    assert deleted is True

    with pytest.raises(KeyError):
        _run(a.read_chunk(ref))


def test_list_chunks():
    # Use a valid PCHK chunk so manifest_id decoding works.
    # NtpInflightAdapter defaults to max_chunk_bytes=16; use a larger instance.
    a = NtpInflightAdapter(max_chunk_bytes=200)
    pchk = "PCHK:1:ntpmid:0:0:1:YQ=="
    with patch.object(a, "_send_udp", return_value=None):
        _run(a.write_chunk(pchk))
    refs = _run(a.list_chunks("ntpmid"))
    assert len(refs) == 1


def test_chunk_too_large():
    a = NtpInflightAdapter(max_chunk_bytes=2)
    with pytest.raises(ValueError):
        _run(a.write_chunk("toolong"))
