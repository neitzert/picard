"""Unit tests for lightning_adapter (pyln mocked)."""
import asyncio
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from picard.adapters.lightning_adapter import LightningAdapter
from picard.adapters.base import StoredChunkRef

CHUNK = "hi"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_profile():
    a = LightningAdapter()
    p = a.profile()
    assert p.name == "lightning"


def test_write_read_delete():
    a = LightningAdapter()
    mock_rpc = MagicMock()
    mock_rpc.call = MagicMock(return_value={})
    mock_rpc.cancelinvoice = MagicMock(return_value={})

    with patch.object(a, "_rpc", return_value=mock_rpc), \
         patch("asyncio.to_thread", new=AsyncMock(side_effect=lambda f, *a, **kw: f())):
        ref = _run(a.write_chunk(CHUNK))
    assert ref.adapter_name == "lightning"
    assert ref.location.startswith("lightning://")

    # read is from in-process registry -- no RPC needed
    data = _run(a.read_chunk(ref))
    assert data == CHUNK

    with patch.object(a, "_rpc", return_value=mock_rpc), \
         patch("asyncio.to_thread", new=AsyncMock(side_effect=lambda f, *a, **kw: f())):
        deleted = _run(a.delete_chunk(ref))
    assert deleted is True

    with pytest.raises(KeyError):
        _run(a.read_chunk(ref))


def test_list_chunks():
    a = LightningAdapter()
    mock_rpc = MagicMock()
    mock_rpc.call = MagicMock(return_value={})
    pchk = "PCHK:1:lnmid:0:0:1:YQ=="
    with patch.object(a, "_rpc", return_value=mock_rpc), \
         patch("asyncio.to_thread", new=AsyncMock(side_effect=lambda f, *args, **kw: f())):
        _run(a.write_chunk(pchk))
    refs = _run(a.list_chunks("lnmid"))
    assert len(refs) == 1


def test_health_check_no_rpc():
    a = LightningAdapter()
    # No CLN node -- health_check should return False, not raise
    with patch("asyncio.to_thread", new=AsyncMock(return_value=False)):
        result = _run(a.health_check())
    assert result is False
