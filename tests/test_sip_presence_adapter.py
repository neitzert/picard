"""Tests for SipPresenceAdapter.

All UDP socket I/O is mocked via unittest.mock.patch. No real SIP server or
network connection is required.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.sip_presence_adapter import (
    SipPresenceAdapter,
    _build_pidf,
    _parse_pidf,
    _parse_status,
    _parse_header,
    _ADAPTER_NAME,
    _PICARD_NS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_200_publish(etag: str = "etag-abc") -> str:
    return (
        "SIP/2.0 200 OK\r\n"
        f"SIP-ETag: {etag}\r\n"
        "Expires: 3600\r\n"
        "Content-Length: 0\r\n"
        "\r\n"
    )


def _make_200_subscribe(payload_b64: str, data_len: int) -> str:
    body = _build_pidf(
        entity="picard-test@example.local",
        payload_b64=payload_b64,
        data_len=data_len,
    )
    return (
        "SIP/2.0 200 OK\r\n"
        "Content-Type: application/pidf+xml\r\n"
        f"Content-Length: {len(body)}\r\n"
        "\r\n"
        + body
    )


def _make_adapter(**kwargs) -> SipPresenceAdapter:
    defaults = dict(
        presence_server="127.0.0.1",
        port=5060,
        domain="example.local",
        aor_prefix="picard",
        ttl=3600,
    )
    defaults.update(kwargs)
    return SipPresenceAdapter(**defaults)


# ---------------------------------------------------------------------------
# Unit tests: PIDF builder and parser
# ---------------------------------------------------------------------------

class TestPidf:
    def test_build_contains_payload(self):
        doc = _build_pidf("user@host", "SGVsbG8", 5)
        assert "SGVsbG8" in doc
        assert f'len="5"' in doc
        assert "picard:data" in doc

    def test_parse_roundtrip(self):
        doc = _build_pidf("user@host", "SGVsbG8", 5)
        result = _parse_pidf(doc)
        assert result is not None
        payload_b64, data_len = result
        assert payload_b64 == "SGVsbG8"
        assert data_len == 5

    def test_parse_missing_data_element(self):
        doc = '<?xml version="1.0"?><presence xmlns="urn:ietf:params:xml:ns:pidf"></presence>'
        assert _parse_pidf(doc) is None

    def test_parse_invalid_xml(self):
        assert _parse_pidf("not xml at all") is None

    def test_build_valid_xml_structure(self):
        import xml.etree.ElementTree as ET
        doc = _build_pidf("test@example.com", "dGVzdA", 4)
        root = ET.fromstring(doc)
        assert root.tag.endswith("presence")


# ---------------------------------------------------------------------------
# Integration tests: write / read / delete
# ---------------------------------------------------------------------------

class TestWriteRead:
    @pytest.mark.asyncio
    async def test_write_returns_ref(self):
        with patch(
            "picard.adapters.sip_presence_adapter._sip_transaction",
            new=AsyncMock(return_value=_make_200_publish()),
        ):
            adapter = _make_adapter()
            ref = await adapter.write_chunk("hello presence")
            assert ref.adapter_name == _ADAPTER_NAME
            loc = json.loads(ref.location)
            assert "aor" in loc
            assert "etag" in loc

    @pytest.mark.asyncio
    async def test_write_sends_pidf_body(self):
        sent: list[bytes] = []

        async def capture(host, port, msg, timeout=5.0):
            sent.append(msg)
            return _make_200_publish()

        with patch("picard.adapters.sip_presence_adapter._sip_transaction", new=capture):
            adapter = _make_adapter()
            await adapter.write_chunk("test data")
            publish_msg = sent[0].decode()
            assert "PUBLISH" in publish_msg
            assert "application/pidf+xml" in publish_msg
            assert "picard:data" in publish_msg

    @pytest.mark.asyncio
    async def test_write_encodes_data_in_pidf(self):
        sent: list[bytes] = []

        async def capture(host, port, msg, timeout=5.0):
            sent.append(msg)
            return _make_200_publish()

        with patch("picard.adapters.sip_presence_adapter._sip_transaction", new=capture):
            adapter = _make_adapter()
            await adapter.write_chunk("encode me")
            msg_text = sent[0].decode()
            expected = base64.urlsafe_b64encode(b"encode me").decode().rstrip("=")
            assert expected in msg_text

    @pytest.mark.asyncio
    async def test_read_returns_original_data(self):
        original = "hello from sip presence"
        payload_b64 = base64.urlsafe_b64encode(original.encode()).decode().rstrip("=")
        ref = StoredChunkRef(
            adapter_name=_ADAPTER_NAME,
            location=json.dumps({
                "aor": "picard-abc@example.local",
                "call_id": "testid@picard",
                "from_tag": "abcd1234",
                "etag": "etag-xyz",
                "server": "127.0.0.1:5060",
                "data_len": len(original),
            }),
        )
        with patch(
            "picard.adapters.sip_presence_adapter._sip_transaction",
            new=AsyncMock(return_value=_make_200_subscribe(payload_b64, len(original))),
        ):
            adapter = _make_adapter()
            result = await adapter.read_chunk(ref)
            assert result == original

    @pytest.mark.asyncio
    async def test_read_sends_subscribe(self):
        sent: list[bytes] = []
        original = "sub test"
        payload_b64 = base64.urlsafe_b64encode(original.encode()).decode().rstrip("=")

        async def capture(host, port, msg, timeout=5.0):
            sent.append(msg)
            return _make_200_subscribe(payload_b64, len(original))

        ref = StoredChunkRef(
            adapter_name=_ADAPTER_NAME,
            location=json.dumps({
                "aor": "picard-abc@example.local",
                "call_id": "x",
                "from_tag": "y",
                "etag": None,
                "server": "127.0.0.1:5060",
                "data_len": len(original),
            }),
        )
        with patch("picard.adapters.sip_presence_adapter._sip_transaction", new=capture):
            adapter = _make_adapter()
            await adapter.read_chunk(ref)
            msg_text = sent[0].decode()
            assert "SUBSCRIBE" in msg_text
            assert "Event: presence" in msg_text

    @pytest.mark.asyncio
    async def test_delete_sends_expires_zero(self):
        sent: list[bytes] = []

        async def capture(host, port, msg, timeout=5.0):
            sent.append(msg)
            return _make_200_publish()

        ref = StoredChunkRef(
            adapter_name=_ADAPTER_NAME,
            location=json.dumps({
                "aor": "picard-abc@example.local",
                "call_id": "x",
                "from_tag": "y",
                "etag": "etag-123",
                "server": "127.0.0.1:5060",
                "data_len": 5,
            }),
        )
        with patch("picard.adapters.sip_presence_adapter._sip_transaction", new=capture):
            adapter = _make_adapter()
            result = await adapter.delete_chunk(ref)
            assert result is True
            assert any(b"Expires: 0" in m for m in sent)

    @pytest.mark.asyncio
    async def test_delete_sends_sip_if_match(self):
        sent: list[bytes] = []

        async def capture(host, port, msg, timeout=5.0):
            sent.append(msg)
            return _make_200_publish()

        ref = StoredChunkRef(
            adapter_name=_ADAPTER_NAME,
            location=json.dumps({
                "aor": "picard-abc@example.local",
                "call_id": "x",
                "from_tag": "y",
                "etag": "my-specific-etag",
                "server": "127.0.0.1:5060",
                "data_len": 5,
            }),
        )
        with patch("picard.adapters.sip_presence_adapter._sip_transaction", new=capture):
            adapter = _make_adapter()
            await adapter.delete_chunk(ref)
            publish_msg = sent[0].decode()
            assert "SIP-If-Match: my-specific-etag" in publish_msg

    @pytest.mark.asyncio
    async def test_delete_returns_false_on_error(self):
        async def fail(*args, **kwargs):
            raise ConnectionError("unreachable")

        ref = StoredChunkRef(
            adapter_name=_ADAPTER_NAME,
            location=json.dumps({
                "aor": "picard-abc@example.local",
                "call_id": "x",
                "from_tag": "y",
                "etag": None,
                "server": "127.0.0.1:5060",
                "data_len": 0,
            }),
        )
        with patch("picard.adapters.sip_presence_adapter._sip_transaction", new=fail):
            adapter = _make_adapter()
            result = await adapter.delete_chunk(ref)
            assert result is False


class TestHealthAndProfile:
    @pytest.mark.asyncio
    async def test_health_check_true_on_200(self):
        with patch(
            "picard.adapters.sip_presence_adapter._sip_transaction",
            new=AsyncMock(return_value="SIP/2.0 200 OK\r\nContent-Length: 0\r\n\r\n"),
        ):
            adapter = _make_adapter()
            assert await adapter.health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_false_on_timeout(self):
        async def fail(*args, **kwargs):
            raise TimeoutError()

        with patch("picard.adapters.sip_presence_adapter._sip_transaction", new=fail):
            adapter = _make_adapter()
            assert await adapter.health_check() is False

    def test_profile_name(self):
        assert _make_adapter().profile().name == _ADAPTER_NAME

    def test_profile_experimental(self):
        tags = _make_adapter().profile().tags
        assert tags.get("experimental") == "true"
        assert tags.get("substrate") == "sip-presence"

    @pytest.mark.asyncio
    async def test_list_chunks_empty(self):
        adapter = _make_adapter()
        result = await adapter.list_chunks("manifest-xyz")
        assert list(result) == []
