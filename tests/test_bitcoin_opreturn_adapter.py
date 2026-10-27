"""Unit tests for bitcoin_opreturn_adapter (in-process registry mocked)."""
import asyncio
import pytest
from picard.adapters.bitcoin_opreturn_adapter import BitcoinOpReturnAdapter
from picard.adapters.base import StoredChunkRef

CHUNK = "hi"
PCHK_CHUNK = "PCHK:1:btcmid:0:0:1:YQ=="
FAKE_TXID = "deadbeef" * 8
FAKE_LOC = f"bitcoin://{FAKE_TXID}"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _adapter_with_chunk(chunk=CHUNK):
    a = BitcoinOpReturnAdapter()
    a._registry[FAKE_LOC] = chunk
    ref = StoredChunkRef(adapter_name="bitcoin_opreturn", location=FAKE_LOC)
    return a, ref


def test_profile():
    a = BitcoinOpReturnAdapter()
    p = a.profile()
    assert p.name == "bitcoin_opreturn"
    assert p.max_chunk_bytes == 80


def test_read_from_registry():
    a, ref = _adapter_with_chunk()
    data = _run(a.read_chunk(ref))
    assert data == CHUNK


def test_delete():
    a, ref = _adapter_with_chunk()
    deleted = _run(a.delete_chunk(ref))
    assert deleted is True
    assert FAKE_LOC not in a._registry


def test_delete_missing():
    a = BitcoinOpReturnAdapter()
    ref = StoredChunkRef(adapter_name="bitcoin_opreturn", location=FAKE_LOC)
    deleted = _run(a.delete_chunk(ref))
    assert deleted is False


def test_list_chunks():
    a, _ = _adapter_with_chunk(PCHK_CHUNK)
    refs = _run(a.list_chunks("btcmid"))
    assert len(refs) == 1
    assert refs[0].location == FAKE_LOC


def test_list_chunks_no_match():
    a, _ = _adapter_with_chunk(PCHK_CHUNK)
    refs = _run(a.list_chunks("othermid"))
    assert len(refs) == 0


def test_health_check_no_rpc():
    a = BitcoinOpReturnAdapter()
    # No bitcoin node running -- returns False, not an exception
    result = _run(a.health_check())
    assert result is False
