"""Tests for SipRegistrarAdapter.

All UDP socket I/O is mocked via unittest.mock.patch. No real SIP server or
network connection is required.
"""

from __future__ import annotations

import asyncio
import base64
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.sip_registrar_adapter import (
    SipRegistrarAdapter,
    _build_register,
    _build_options,
    _parse_status,
    _parse_header,
    _parse_www_authenticate,
    _extract_picard_payload,
    _digest_response,
    _ADAPTER_NAME,
    _PICARD_PARAM,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_200(contact_payload: str | None = None) -> str:
    contact = (
        f"<sip:picard@127.0.0.1:5060;{_PICARD_PARAM}={contact_payload}>"
        if contact_payload
        else "<sip:picard@127.0.0.1:5060>"
    )
    return (
        "SIP/2.0 200 OK\r\n"
        "Via: SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKtest\r\n"
        f"Contact: {contact}\r\n"
        "CSeq: 1 REGISTER\r\n"
        "Content-Length: 0\r\n"
        "\r\n"
    )


def _make_401() -> str:
    return (
        "SIP/2.0 401 Unauthorized\r\n"
        'WWW-Authenticate: Digest realm="example.local", nonce="abc123", algorithm=MD5\r\n'
        "Content-Length: 0\r\n"
        "\r\n"
    )


def _make_options_200() -> str:
    return (
        "SIP/2.0 200 OK\r\n"
        "Allow: INVITE, ACK, BYE, CANCEL, OPTIONS, REGISTER, NOTIFY\r\n"
        "Content-Length: 0\r\n"
        "\r\n"
    )


def _make_adapter(**kwargs) -> SipRegistrarAdapter:
    defaults = dict(
        registrar_host="127.0.0.1",
        registrar_port=5060,
        domain="example.local",
        username="picard",
        password="secret",
        ttl=3600,
    )
    defaults.update(kwargs)
    return SipRegistrarAdapter(**defaults)


# ---------------------------------------------------------------------------
# Unit tests: message builders and parsers
# ---------------------------------------------------------------------------

class TestBuilders:
    def test_parse_status_200(self):
        assert _parse_status("SIP/2.0 200 OK\r\nContent-Length: 0\r\n") == 200

    def test_parse_status_401(self):
        assert _parse_status(_make_401()) == 401

    def test_parse_status_missing(self):
        assert _parse_status("not a sip message") == 0

    def test_parse_header_contact(self):
        resp = _make_200("abc123")
        hdr = _parse_header(resp, "Contact")
        assert hdr is not None
        assert "abc123" in hdr

    def test_extract_picard_payload(self):
        contact = f"<sip:picard@host;{_PICARD_PARAM}=SGVsbG8>"
        assert _extract_picard_payload(contact) == "SGVsbG8"

    def test_extract_picard_payload_missing(self):
        assert _extract_picard_payload("<sip:picard@host>") is None

    def test_digest_response_known_vector(self):
        # RFC 3261 section 24.2 test vector (simplified)
        resp = _digest_response(
            username="bob",
            realm="biloxi.com",
            password="zanzibar",
            method="REGISTER",
            uri="sip:biloxi.com",
            nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093",
        )
        assert len(resp) == 32  # MD5 hex digest

    def test_parse_www_authenticate(self):
        response = _make_401()
        params = _parse_www_authenticate(response)
        assert params["realm"] == "example.local"
        assert params["nonce"] == "abc123"
        assert params["algorithm"] == "MD5"

    def test_build_register_contains_picard_param(self):
        msg = _build_register(
            registrar_host="127.0.0.1",
            registrar_port=5060,
            domain="example.local",
            username="picard",
            aor="picard-abc@example.local",
            call_id="testcallid@picard",
            from_tag="abc12345",
            cseq_n=1,
            expires=3600,
            contact_payload="SGVsbG8",
        )
        text = msg.decode()
        assert "REGISTER sip:example.local" in text
        assert f"{_PICARD_PARAM}=SGVsbG8" in text
        assert "Expires: 3600" in text


# ---------------------------------------------------------------------------
# Integration tests: write / read / delete via mocked _sip_transaction
# ---------------------------------------------------------------------------

class TestWriteRead:
    @pytest.mark.asyncio
    async def test_write_returns_ref(self):
        payload_b64 = base64.urlsafe_b64encode(b"hello picard").decode().rstrip("=")
        with patch(
            "picard.adapters.sip_registrar_adapter._sip_transaction",
            new=AsyncMock(return_value=_make_200(payload_b64)),
        ):
            adapter = _make_adapter()
            ref = await adapter.write_chunk("hello picard")
            assert ref.adapter_name == _ADAPTER_NAME
            loc = json.loads(ref.location)
            assert "aor" in loc
            assert "call_id" in loc
            assert "from_tag" in loc

    @pytest.mark.asyncio
    async def test_write_encodes_data_in_contact(self):
        sent_messages: list[bytes] = []

        async def capture(host, port, msg, timeout=5.0):
            sent_messages.append(msg)
            payload_b64 = base64.urlsafe_b64encode(b"testdata").decode().rstrip("=")
            return _make_200(payload_b64)

        with patch("picard.adapters.sip_registrar_adapter._sip_transaction", new=capture):
            adapter = _make_adapter()
            await adapter.write_chunk("testdata")
            register_msg = sent_messages[0].decode()
            assert _PICARD_PARAM in register_msg
            expected_b64 = base64.urlsafe_b64encode(b"testdata").decode().rstrip("=")
            assert expected_b64 in register_msg

    @pytest.mark.asyncio
    async def test_read_returns_original_data(self):
        original = "hello from sip registrar"
        payload_b64 = base64.urlsafe_b64encode(original.encode()).decode().rstrip("=")
        ref = StoredChunkRef(
            adapter_name=_ADAPTER_NAME,
            location=json.dumps({
                "aor": "picard-abc@example.local",
                "call_id": "testid@picard",
                "from_tag": "abcd1234",
                "registrar": "127.0.0.1:5060",
                "data_len": len(original),
            }),
        )
        with patch(
            "picard.adapters.sip_registrar_adapter._sip_transaction",
            new=AsyncMock(return_value=_make_200(payload_b64)),
        ):
            adapter = _make_adapter()
            result = await adapter.read_chunk(ref)
            assert result == original

    @pytest.mark.asyncio
    async def test_delete_sends_expires_zero(self):
        sent_messages: list[bytes] = []

        async def capture(host, port, msg, timeout=5.0):
            sent_messages.append(msg)
            return _make_200()

        ref = StoredChunkRef(
            adapter_name=_ADAPTER_NAME,
            location=json.dumps({
                "aor": "picard-abc@example.local",
                "call_id": "testid@picard",
                "from_tag": "abcd1234",
                "registrar": "127.0.0.1:5060",
                "data_len": 5,
            }),
        )
        with patch("picard.adapters.sip_registrar_adapter._sip_transaction", new=capture):
            adapter = _make_adapter()
            result = await adapter.delete_chunk(ref)
            assert result is True
            assert any(b"Expires: 0" in m for m in sent_messages)

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
                "registrar": "127.0.0.1:5060",
                "data_len": 0,
            }),
        )
        with patch("picard.adapters.sip_registrar_adapter._sip_transaction", new=fail):
            adapter = _make_adapter()
            result = await adapter.delete_chunk(ref)
            assert result is False


class TestDigestAuth:
    @pytest.mark.asyncio
    async def test_write_retries_on_401(self):
        call_count = 0

        async def respond(host, port, msg, timeout=5.0):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_401()
            payload_b64 = base64.urlsafe_b64encode(b"authtest").decode().rstrip("=")
            return _make_200(payload_b64)

        with patch("picard.adapters.sip_registrar_adapter._sip_transaction", new=respond):
            adapter = _make_adapter()
            ref = await adapter.write_chunk("authtest")
            assert call_count == 2
            assert ref.adapter_name == _ADAPTER_NAME

    @pytest.mark.asyncio
    async def test_auth_header_contains_digest(self):
        sent: list[bytes] = []
        call_count = 0

        async def respond(host, port, msg, timeout=5.0):
            nonlocal call_count
            call_count += 1
            sent.append(msg)
            if call_count == 1:
                return _make_401()
            payload_b64 = base64.urlsafe_b64encode(b"x").decode().rstrip("=")
            return _make_200(payload_b64)

        with patch("picard.adapters.sip_registrar_adapter._sip_transaction", new=respond):
            adapter = _make_adapter()
            await adapter.write_chunk("x")
            second_msg = sent[1].decode()
            assert "Authorization: Digest" in second_msg
            assert 'username="picard"' in second_msg


class TestHealthAndProfile:
    @pytest.mark.asyncio
    async def test_health_check_true_on_200(self):
        with patch(
            "picard.adapters.sip_registrar_adapter._sip_transaction",
            new=AsyncMock(return_value=_make_options_200()),
        ):
            adapter = _make_adapter()
            assert await adapter.health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_false_on_error(self):
        async def fail(*args, **kwargs):
            raise TimeoutError()

        with patch("picard.adapters.sip_registrar_adapter._sip_transaction", new=fail):
            adapter = _make_adapter()
            assert await adapter.health_check() is False

    def test_profile_name(self):
        adapter = _make_adapter()
        assert adapter.profile().name == _ADAPTER_NAME

    def test_profile_experimental_tag(self):
        adapter = _make_adapter()
        tags = adapter.profile().tags
        assert tags.get("experimental") == "true"

    @pytest.mark.asyncio
    async def test_list_chunks_empty(self):
        adapter = _make_adapter()
        result = await adapter.list_chunks("manifest-xyz")
        assert list(result) == []
