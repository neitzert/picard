"""Tests for LlmContextAdapter."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from picard.adapters.llm_context_adapter import LlmContextAdapter


def make_chunk(manifest_id: str = "test-manifest", idx: int = 0) -> str:
    import base64, os
    payload = base64.urlsafe_b64encode(os.urandom(12)).decode()
    return f"PCHK:2:{manifest_id}:{idx}:{idx}:1:{payload}"


@pytest.fixture
def adapter():
    return LlmContextAdapter(ollama_url="http://localhost:11434", model="llama3.2")


@pytest.mark.asyncio
async def test_health_check_no_ollama(adapter):
    """health_check returns False when Ollama is not running."""
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=Exception("connection refused"))
        mock_client_cls.return_value = mock_client
        result = await adapter.health_check()
    assert result is False


@pytest.mark.asyncio
async def test_health_check_with_ollama(adapter):
    """health_check returns True when Ollama responds 200."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client
        result = await adapter.health_check()
    assert result is True


@pytest.mark.asyncio
async def test_profile_fields(adapter):
    p = adapter.profile()
    assert p.name == "llm-context"
    assert p.max_chunk_bytes == 65536
    assert p.persistent is False
    assert p.ordered is False


@pytest.mark.asyncio
async def test_write_read_roundtrip(adapter):
    chunk = make_chunk(idx=0)
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=Exception("no ollama"))
        mock_client_cls.return_value = mock_client
        ref = await adapter.write_chunk(chunk)
    assert ref.adapter_name == "llm-context"
    assert "llm-context://" in ref.location
    result = await adapter.read_chunk(ref)
    assert result == chunk


@pytest.mark.asyncio
async def test_write_works_without_ollama(adapter):
    """write_chunk succeeds via in-process fallback even when Ollama is unreachable."""
    chunk = make_chunk(idx=0)
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=Exception("no ollama"))
        mock_client_cls.return_value = mock_client
        ref = await adapter.write_chunk(chunk)
    assert ref is not None


@pytest.mark.asyncio
async def test_delete_prevents_read(adapter):
    chunk = make_chunk(idx=1)
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=Exception("no ollama"))
        mock_client_cls.return_value = mock_client
        ref = await adapter.write_chunk(chunk)
    deleted = await adapter.delete_chunk(ref)
    assert deleted is True
    with pytest.raises(KeyError):
        await adapter.read_chunk(ref)


@pytest.mark.asyncio
async def test_list_chunks_by_manifest_id(adapter):
    mid = "llm-manifest"

    async def do_write(chunk):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=Exception("no ollama"))
            mock_client_cls.return_value = mock_client
            return await adapter.write_chunk(chunk)

    ref1 = await do_write(make_chunk(mid, 0))
    ref2 = await do_write(make_chunk(mid, 1))
    await do_write(make_chunk("other", 0))

    listed = await adapter.list_chunks(mid)
    locs = {r.location for r in listed}
    assert ref1.location in locs
    assert ref2.location in locs


@pytest.mark.asyncio
async def test_delete_nonexistent_returns_false(adapter):
    from picard.adapters.base import StoredChunkRef
    ref = StoredChunkRef(adapter_name="llm-context", location="llm-context://deadbeef12345678")
    result = await adapter.delete_chunk(ref)
    assert result is False
