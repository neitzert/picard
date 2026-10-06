"""Tests for the WPAD adapter."""

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.wpad_adapter import WpadAdapter


@pytest.fixture
def adapter():
    return WpadAdapter(
        wpad_host="wpad.corp.local",
        wpad_port=80,
    )


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "wpad"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 4096


class TestEncoding:
    def test_encode_decode_roundtrip(self, adapter):
        key = "test_key"
        data = "hello-picard"
        comment = adapter._encode_comment(key, data)
        assert "PICARD:" in comment
        decoded = adapter._decode_comments(comment)
        assert decoded[key] == data

    def test_multiple_chunks(self, adapter):
        c1 = adapter._encode_comment("k1", "data1")
        c2 = adapter._encode_comment("k2", "data2")
        content = f"function FindProxyForURL() {{}}\n{c1}\n{c2}\n"
        decoded = adapter._decode_comments(content)
        assert decoded["k1"] == "data1"
        assert decoded["k2"] == "data2"


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_chunk(self, adapter):
        mock_get_resp = AsyncMock()
        mock_get_resp.status = 200
        mock_get_resp.text = AsyncMock(return_value='function FindProxyForURL() { return "DIRECT"; }')
        mock_get_resp.__aenter__ = AsyncMock(return_value=mock_get_resp)
        mock_get_resp.__aexit__ = AsyncMock(return_value=False)

        mock_put_resp = AsyncMock()
        mock_put_resp.status = 200
        mock_put_resp.raise_for_status = MagicMock()
        mock_put_resp.__aenter__ = AsyncMock(return_value=mock_put_resp)
        mock_put_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_get_resp)
        mock_session.put = MagicMock(return_value=mock_put_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            ref = await adapter.write_chunk("test-data")

        assert ref.adapter_name == "wpad"
        assert ref.location.startswith("wpad://")

    async def test_read_chunk(self, adapter):
        chunk_data = "test-chunk-content"
        b64 = base64.b64encode(chunk_data.encode()).decode()
        pac = f'function FindProxyForURL() {{}}\n/* PICARD:mykey:{b64}:DRACP */\n'

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = AsyncMock(return_value=pac)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        from picard.adapters.base import StoredChunkRef

        ref = StoredChunkRef(adapter_name="wpad", location="wpad://wpad.corp.local/mykey")

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await adapter.read_chunk(ref)

        assert result == chunk_data

    async def test_read_missing_raises(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = AsyncMock(return_value='function FindProxyForURL() {}')
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        from picard.adapters.base import StoredChunkRef

        ref = StoredChunkRef(adapter_name="wpad", location="wpad://wpad.corp.local/nope")
        with patch("aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(KeyError):
                await adapter.read_chunk(ref)


@pytest.mark.asyncio
class TestHealth:
    async def test_healthy(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            assert await adapter.health_check() is True

    async def test_unhealthy(self, adapter):
        with patch("aiohttp.ClientSession", side_effect=Exception("fail")):
            assert await adapter.health_check() is False
