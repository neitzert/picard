"""Unit tests for tcp_isn_adapter (in-process fallback, no companion)."""
import asyncio
import pytest
from unittest.mock import patch
from picard.adapters.tcp_isn_adapter import TcpIsnAdapter
from picard.adapters.base import StoredChunkRef

CHUNK = "hi"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _adapter():
    # Use a socket path that doesn't exist so companion_available() returns False
    return TcpIsnAdapter(companion_socket="/tmp/picard_tcp_isn_nonexistent.sock")


def test_profile():
    a = _adapter()
    p = a.profile()
    assert p.name == "tcp-isn"


def test_health_check():
    a = _adapter()
    result = _run(a.health_check())
    assert result is True


def test_write_read_delete():
    a = _adapter()
    # Patch scapy send so no actual packets go out
    with patch("picard.adapters.tcp_isn_adapter._SCAPY_OK", False):
        ref = _run(a.write_chunk(CHUNK))
    assert ref.adapter_name == "tcp-isn"

    with patch("picard.adapters.tcp_isn_adapter._SCAPY_OK", False):
        data = _run(a.read_chunk(ref))
    assert data == CHUNK

    deleted = _run(a.delete_chunk(ref))
    assert deleted is True

    with pytest.raises(KeyError):
        _run(a.read_chunk(ref))


def test_list_chunks():
    a = _adapter()
    with patch("picard.adapters.tcp_isn_adapter._SCAPY_OK", False):
        _run(a.write_chunk(CHUNK))
    refs = _run(a.list_chunks(""))
    assert len(refs) == 1
