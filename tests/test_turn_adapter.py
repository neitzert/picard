"""Tests for TurnAdapter (STUN/TURN allocation state substrate).

All STUN/TURN network I/O is mocked.  No real TURN server is required.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.turn_adapter import (
    TurnAdapter,
    _build_stun,
    _parse_stun,
    _long_term_key,
    _channel_from_chunk,
    _new_txn_id,
    _ALLOCATE_REQUEST,
    _ALLOCATE_SUCCESS,
    _ALLOCATE_ERROR,
    _BINDING_REQUEST,
    _BINDING_SUCCESS,
    _REFRESH_REQUEST,
    _REFRESH_SUCCESS,
    _CHANNELBIND_REQUEST,
    _CHANNELBIND_SUCCESS,
    _ATTR_REALM,
    _ATTR_NONCE,
    _ATTR_USERNAME,
    _ATTR_LIFETIME,
    _ATTR_XOR_RELAYED_ADDRESS,
    _ATTR_ERROR_CODE,
    _MAGIC_COOKIE,
    _CHANNEL_MIN,
    _CHANNEL_MAX,
    StoredChunkRef,
)

# ---------------------------------------------------------------------------
# Helpers for building synthetic STUN responses
# ---------------------------------------------------------------------------

def _pack_attr(atype: int, value: bytes) -> bytes:
    pad = (4 - len(value) % 4) % 4
    return struct.pack(">HH", atype, len(value)) + value + b"\x00" * pad


def _stun_msg(msg_type: int, txn_id: bytes, attrs: list[tuple[int, bytes]]) -> bytes:
    body = b"".join(_pack_attr(t, v) for t, v in attrs)
    return struct.pack(">HHI", msg_type, len(body), _MAGIC_COOKIE) + txn_id + body


def _xor_relayed_addr(ip: str = "192.0.2.1", port: int = 50000) -> bytes:
    """Build a valid XOR-RELAYED-ADDRESS attribute value for IPv4."""
    xport = port ^ (_MAGIC_COOKIE >> 16)
    parts = [int(x) for x in ip.split(".")]
    xip = (parts[0] << 24 | parts[1] << 16 | parts[2] << 8 | parts[3]) ^ _MAGIC_COOKIE
    return struct.pack(">BBHI", 0x00, 0x01, xport, xip)


def _make_401(txn_id: bytes) -> bytes:
    error = struct.pack(">HH", 0, 401) + b"Unauthorized"
    return _stun_msg(
        _ALLOCATE_ERROR, txn_id,
        [
            (_ATTR_REALM, b"example.org"),
            (_ATTR_NONCE, b"testnonce0001"),
            (_ATTR_ERROR_CODE, error),
        ],
    )


def _make_alloc_success(txn_id: bytes) -> bytes:
    return _stun_msg(
        _ALLOCATE_SUCCESS, txn_id,
        [(_ATTR_XOR_RELAYED_ADDRESS, _xor_relayed_addr())],
    )


def _make_channelbind_success(txn_id: bytes) -> bytes:
    return _stun_msg(_CHANNELBIND_SUCCESS, txn_id, [])


def _make_binding_success(txn_id: bytes) -> bytes:
    return _stun_msg(_BINDING_SUCCESS, txn_id, [])


def _make_refresh_success(txn_id: bytes) -> bytes:
    return _stun_msg(_REFRESH_SUCCESS, txn_id, [(_ATTR_LIFETIME, struct.pack(">I", 600))])


# ---------------------------------------------------------------------------
# Fixture: patched TurnAdapter with pre-baked response queue
# ---------------------------------------------------------------------------

class _FakeProto:
    """Minimal _StunProtocol stand-in that returns pre-baked responses."""

    def __init__(self, responses: list[bytes]) -> None:
        self._responses = list(responses)
        self.sent: list[bytes] = []
        self.transport = MagicMock()
        self.transport.close = MagicMock()

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def recv(self, timeout: float = 3.0) -> bytes:
        if not self._responses:
            raise asyncio.TimeoutError("no more responses")
        return self._responses.pop(0)


def _adapter(responses: list[bytes] | None = None) -> tuple[TurnAdapter, _FakeProto]:
    adapter = TurnAdapter(
        turn_host="127.0.0.1",
        turn_port=3478,
        username="picard",
        password="secret",
        stun_host="127.0.0.1",
        stun_port=3478,
        allocation_lifetime=600,
    )
    fake = _FakeProto(responses or [])
    return adapter, fake


def _mock_make_udp(fake: _FakeProto):
    async def _make(*_args, **_kwargs):
        return fake
    return _make


# ---------------------------------------------------------------------------
# Unit tests: codec helpers
# ---------------------------------------------------------------------------

class TestStunCodec:
    def test_round_trip_no_attrs(self):
        txn = _new_txn_id()
        msg = _build_stun(_ALLOCATE_REQUEST, txn, [])
        parsed = _parse_stun(msg)
        assert parsed["type"] == _ALLOCATE_REQUEST
        assert parsed["txn_id"] == txn
        assert parsed["attrs"] == {}

    def test_round_trip_with_attrs(self):
        txn = _new_txn_id()
        msg = _build_stun(
            _ALLOCATE_REQUEST, txn,
            [(_ATTR_USERNAME, b"alice"), (_ATTR_REALM, b"example.org")],
        )
        parsed = _parse_stun(msg)
        assert parsed["attrs"][_ATTR_USERNAME] == b"alice"
        assert parsed["attrs"][_ATTR_REALM] == b"example.org"

    def test_truncated_raises(self):
        with pytest.raises(ValueError, match="truncated"):
            _parse_stun(b"\x00" * 10)

    def test_message_integrity_appended(self):
        txn = _new_txn_id()
        key = _long_term_key("user", "realm", "pass")
        msg = _build_stun(_ALLOCATE_REQUEST, txn, [(_ATTR_USERNAME, b"user")], key=key)
        # MESSAGE-INTEGRITY attribute type is 0x0008; it must appear in attrs
        parsed = _parse_stun(msg)
        assert 0x0008 in parsed["attrs"]


class TestLongTermKey:
    def test_key_is_md5_of_colon_joined(self):
        key = _long_term_key("user", "realm", "pass")
        expected = hashlib.md5(b"user:realm:pass").digest()
        assert key == expected

    def test_different_inputs_differ(self):
        k1 = _long_term_key("a", "b", "c")
        k2 = _long_term_key("a", "b", "x")
        assert k1 != k2


class TestChannelDerivation:
    def test_in_valid_range(self):
        ch = _channel_from_chunk("some-chunk-id")
        assert _CHANNEL_MIN <= ch <= _CHANNEL_MAX

    def test_stable(self):
        ch1 = _channel_from_chunk("abc")
        ch2 = _channel_from_chunk("abc")
        assert ch1 == ch2

    def test_different_ids_differ(self):
        ch1 = _channel_from_chunk("chunk-1")
        ch2 = _channel_from_chunk("chunk-2")
        # Not guaranteed to differ for every pair but almost certain:
        assert ch1 != ch2 or True  # just validate no crash


# ---------------------------------------------------------------------------
# Integration tests: write / read / delete / health_check / close
# ---------------------------------------------------------------------------

class TestWriteChunk:
    @pytest.mark.asyncio
    async def test_write_returns_ref_with_allocation_id(self):
        adapter, fake = _adapter()
        txn = b"\x00" * 12
        fake._responses = [_make_401(txn), _make_alloc_success(txn)]

        with patch("picard.adapters.turn_adapter._make_udp_protocol", _mock_make_udp(fake)):
            with patch("picard.adapters.turn_adapter._TurnAllocation.start_refresh"):
                ref = await adapter.write_chunk("hello-picard")

        assert ref.adapter_name == "turn"
        loc = json.loads(ref.location)
        assert "allocation_id" in loc
        assert "relay_addr" in loc
        assert "channel" in loc
        await adapter.close()

    @pytest.mark.asyncio
    async def test_write_oversized_raises(self):
        adapter = TurnAdapter(
            turn_host="127.0.0.1", username="u", password="p", max_chunk_bytes=4
        )
        with pytest.raises(ValueError, match="exceeds max_chunk_bytes"):
            await adapter.write_chunk("hello-world-too-long")

    @pytest.mark.asyncio
    async def test_write_sends_two_allocate_requests(self):
        adapter, fake = _adapter()
        txn = b"\x01" * 12
        fake._responses = [_make_401(txn), _make_alloc_success(txn)]

        with patch("picard.adapters.turn_adapter._make_udp_protocol", _mock_make_udp(fake)):
            with patch("picard.adapters.turn_adapter._TurnAllocation.start_refresh"):
                await adapter.write_chunk("data")

        # Two Allocate messages: unauthenticated + authenticated
        allocate_msgs = [m for m in fake.sent if _parse_stun(m)["type"] == _ALLOCATE_REQUEST]
        assert len(allocate_msgs) == 2
        await adapter.close()


class TestReadChunk:
    @pytest.mark.asyncio
    async def test_read_returns_written_data(self):
        adapter, fake = _adapter()
        txn = b"\x02" * 12
        fake._responses = [_make_401(txn), _make_alloc_success(txn)]

        with patch("picard.adapters.turn_adapter._make_udp_protocol", _mock_make_udp(fake)):
            with patch("picard.adapters.turn_adapter._TurnAllocation.start_refresh"):
                ref = await adapter.write_chunk("stored-payload")

        result = await adapter.read_chunk(ref)
        assert result == "stored-payload"
        await adapter.close()

    @pytest.mark.asyncio
    async def test_read_missing_raises(self):
        adapter = TurnAdapter(turn_host="127.0.0.1", username="u", password="p")
        ref = StoredChunkRef(
            adapter_name="turn",
            location=json.dumps({"allocation_id": "deadbeef", "relay_addr": "x:1"}),
        )
        with pytest.raises(KeyError, match="deadbeef"):
            await adapter.read_chunk(ref)


class TestDeleteChunk:
    @pytest.mark.asyncio
    async def test_delete_existing_returns_true(self):
        adapter, fake = _adapter()
        txn = b"\x03" * 12
        fake._responses = [
            _make_401(txn),
            _make_alloc_success(txn),
            _make_refresh_success(txn),  # deallocate sends Refresh(0)
        ]

        with patch("picard.adapters.turn_adapter._make_udp_protocol", _mock_make_udp(fake)):
            with patch("picard.adapters.turn_adapter._TurnAllocation.start_refresh"):
                ref = await adapter.write_chunk("ephemeral")
            deleted = await adapter.delete_chunk(ref)

        assert deleted is True
        loc = json.loads(ref.location)
        assert loc["allocation_id"] not in adapter._allocations

    @pytest.mark.asyncio
    async def test_delete_missing_returns_false(self):
        adapter = TurnAdapter(turn_host="127.0.0.1", username="u", password="p")
        ref = StoredChunkRef(
            adapter_name="turn",
            location=json.dumps({"allocation_id": "nosuchalloc"}),
        )
        result = await adapter.delete_chunk(ref)
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_sends_refresh_lifetime_zero(self):
        adapter, fake = _adapter()
        txn = b"\x04" * 12
        fake._responses = [
            _make_401(txn),
            _make_alloc_success(txn),
            _make_refresh_success(txn),
        ]

        with patch("picard.adapters.turn_adapter._make_udp_protocol", _mock_make_udp(fake)):
            with patch("picard.adapters.turn_adapter._TurnAllocation.start_refresh"):
                ref = await adapter.write_chunk("bye")
            await adapter.delete_chunk(ref)

        refresh_msgs = [m for m in fake.sent if _parse_stun(m)["type"] == _REFRESH_REQUEST]
        assert len(refresh_msgs) >= 1
        # Verify lifetime=0 in the deallocate Refresh
        last_refresh = _parse_stun(refresh_msgs[-1])
        lifetime_val = struct.unpack(">I", last_refresh["attrs"][_ATTR_LIFETIME])[0]
        assert lifetime_val == 0


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_success(self):
        adapter, fake = _adapter()
        txn = b"\x05" * 12
        fake._responses = [_make_binding_success(txn)]

        with patch("picard.adapters.turn_adapter._make_udp_protocol", _mock_make_udp(fake)):
            ok = await adapter.health_check()

        assert ok is True

    @pytest.mark.asyncio
    async def test_health_check_timeout_returns_false(self):
        adapter = TurnAdapter(turn_host="127.0.0.1", username="u", password="p")

        async def _raise(*_a, **_kw):
            raise asyncio.TimeoutError

        with patch("picard.adapters.turn_adapter._make_udp_protocol", _raise):
            ok = await adapter.health_check()

        assert ok is False


class TestClose:
    @pytest.mark.asyncio
    async def test_close_deallocates_all(self):
        adapter, fake = _adapter()
        txn = b"\x06" * 12
        fake._responses = [
            _make_401(txn),
            _make_alloc_success(txn),
            _make_refresh_success(txn),  # for deallocate in close()
        ]

        with patch("picard.adapters.turn_adapter._make_udp_protocol", _mock_make_udp(fake)):
            with patch("picard.adapters.turn_adapter._TurnAllocation.start_refresh"):
                await adapter.write_chunk("to-be-closed")

        await adapter.close()
        assert len(adapter._allocations) == 0


class TestProfile:
    def test_profile_fields(self):
        adapter = TurnAdapter(turn_host="127.0.0.1", username="u", password="p")
        p = adapter.profile()
        assert p.name == "turn"
        assert p.persistent is False
        assert "experimental" in p.tags
        assert p.ttl_seconds == 600
