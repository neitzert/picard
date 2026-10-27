"""Unit tests for dns_question_adapter (in-process fallback, no companion)."""
import asyncio
import pytest
from picard.adapters.dns_question_adapter import DnsQuestionAdapter
from picard.adapters.base import StoredChunkRef

CHUNK = "hi"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _adapter():
    return DnsQuestionAdapter(
        companion_socket="/tmp/picard_dns_nonexistent.sock",
        max_chunk_bytes=200,
    )


def test_profile():
    a = _adapter()
    p = a.profile()
    assert p.name == "dns-question"


def test_health_check():
    a = _adapter()
    result = _run(a.health_check())
    assert result is True


def test_write_read_delete():
    a = _adapter()
    ref = _run(a.write_chunk(CHUNK))
    assert ref.adapter_name == "dns-question"
    assert ref.location.startswith("dns://")

    data = _run(a.read_chunk(ref))
    assert data == CHUNK

    deleted = _run(a.delete_chunk(ref))
    assert deleted is True

    with pytest.raises(KeyError):
        _run(a.read_chunk(ref))


def test_list_chunks():
    a = _adapter()
    _run(a.write_chunk(CHUNK))
    refs = _run(a.list_chunks(""))
    assert len(refs) == 1
