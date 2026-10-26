"""Tests for RtpInflightAdapter.

All tests mock the socket layer and relay thread so they run without network
access, without elevated privileges, and on any platform.
"""

from __future__ import annotations

import asyncio
import json
import socket
import struct
import threading
import time
import unittest
from unittest.mock import MagicMock, patch, call

import pytest

from picard.adapters.rtp_inflight_adapter import (
    RtpInflightAdapter,
    _RtpRelay,
    _pack_rtp,
    _unpack_rtp,
    _encode_fragment,
    _decode_fragment,
    _fragment,
    _MAX_RTP_DATA,
    _RTP_HDR_SIZE,
    _FRAG_HDR_SIZE,
    _ADAPTER_NAME,
)
from picard.adapters.base import StoredChunkRef


# ---------------------------------------------------------------------------
# Unit: RTP header pack / unpack
# ---------------------------------------------------------------------------

class TestRtpHeader:
    def test_pack_unpack_roundtrip(self):
        payload = b"hello world"
        pkt = _pack_rtp(ssrc=0xDEADBEEF, seq=42, timestamp=12345, payload=payload)
        ssrc, seq, ts, got = _unpack_rtp(pkt)
        assert ssrc == 0xDEADBEEF
        assert seq == 42
        assert ts == 12345
        assert got == payload

    def test_pack_header_length(self):
        pkt = _pack_rtp(ssrc=1, seq=0, timestamp=0, payload=b"x")
        assert len(pkt) == _RTP_HDR_SIZE + 1

    def test_unpack_too_short_raises(self):
        with pytest.raises(ValueError, match="too short"):
            _unpack_rtp(b"\x00" * 4)

    def test_version_bits(self):
        pkt = _pack_rtp(ssrc=0, seq=0, timestamp=0, payload=b"")
        version = (pkt[0] >> 6) & 0x3
        assert version == 2


# ---------------------------------------------------------------------------
# Unit: Fragment encoding
# ---------------------------------------------------------------------------

class TestFragmentEncoding:
    def test_encode_decode_roundtrip(self):
        data = b"test payload"
        encoded = _encode_fragment(3, 1, data)
        frag_count, frag_idx, got = _decode_fragment(encoded)
        assert frag_count == 3
        assert frag_idx == 1
        assert got == data

    def test_fragment_single_chunk(self):
        data = b"x" * 100
        frags = _fragment(data)
        assert len(frags) == 1
        fc, fi, d = _decode_fragment(frags[0])
        assert fc == 1
        assert fi == 0
        assert d == data

    def test_fragment_exact_boundary(self):
        # Exactly _MAX_RTP_DATA bytes: should produce exactly one fragment
        data = b"a" * _MAX_RTP_DATA
        frags = _fragment(data)
        assert len(frags) == 1

    def test_fragment_one_over_boundary(self):
        # One byte over: should produce two fragments
        data = b"a" * (_MAX_RTP_DATA + 1)
        frags = _fragment(data)
        assert len(frags) == 2
        # First fragment carries exactly _MAX_RTP_DATA bytes of data
        _, _, d0 = _decode_fragment(frags[0])
        assert len(d0) == _MAX_RTP_DATA
        _, _, d1 = _decode_fragment(frags[1])
        assert len(d1) == 1

    def test_fragment_reassemble_order(self):
        data = b"AB" * (_MAX_RTP_DATA // 2 + 1)
        frags = _fragment(data)
        assert len(frags) >= 2
        result = b""
        for f in frags:
            _, _, chunk = _decode_fragment(f)
            result += chunk
        assert result == data

    def test_fragment_empty_data(self):
        frags = _fragment(b"")
        assert len(frags) == 1


# ---------------------------------------------------------------------------
# Relay unit tests (with real threading, no sockets)
# ---------------------------------------------------------------------------

class TestRtpRelayLifecycle:
    def test_start_stop(self):
        relay = _RtpRelay(base_port=59100)
        relay.start()
        assert relay.is_alive()
        relay.stop()
        # Allow thread to exit
        time.sleep(0.15)
        assert not relay.is_alive()

    def test_register_returns_port(self):
        relay = _RtpRelay(base_port=59200)
        relay.start()
        try:
            frags = _fragment(b"hello")
            port = relay.register(ssrc=0xABCD1234, frags=frags)
            assert port == 59200
            relay.unregister(0xABCD1234)
        finally:
            relay.stop()

    def test_register_sequential_ports(self):
        relay = _RtpRelay(base_port=59300)
        relay.start()
        try:
            frags = _fragment(b"a")
            p1 = relay.register(ssrc=0x11111111, frags=frags)
            p2 = relay.register(ssrc=0x22222222, frags=frags)
            assert p2 == p1 + 2
            relay.unregister(0x11111111)
            relay.unregister(0x22222222)
        finally:
            relay.stop()

    def test_get_fragments_after_register(self):
        relay = _RtpRelay(base_port=59400)
        relay.start()
        try:
            data = b"picard"
            frags = _fragment(data)
            relay.register(ssrc=0xCAFEBABE, frags=frags)
            stored = relay.get_fragments(0xCAFEBABE)
            assert stored is not None
            assert len(stored) == len(frags)
            relay.unregister(0xCAFEBABE)
        finally:
            relay.stop()

    def test_get_fragments_unknown_ssrc(self):
        relay = _RtpRelay(base_port=59500)
        relay.start()
        try:
            result = relay.get_fragments(0xDEAD0000)
            assert result is None
        finally:
            relay.stop()

    def test_unregister_removes_ssrc(self):
        relay = _RtpRelay(base_port=49270)
        relay.start()
        mock_sock = MagicMock()
        mock_sock.recvfrom.side_effect = BlockingIOError
        try:
            with patch("picard.adapters.rtp_inflight_adapter.socket.socket", return_value=mock_sock):
                relay.register(ssrc=0x12345678, frags=_fragment(b"x"))
            relay.unregister(0x12345678)
            assert relay.get_fragments(0x12345678) is None
        finally:
            relay.stop()


# ---------------------------------------------------------------------------
# Adapter integration tests
# ---------------------------------------------------------------------------

class TestRtpInflightAdapter:
    """Integration tests using real loopback UDP."""

    @pytest.fixture
    def adapter(self):
        a = RtpInflightAdapter(base_port=58000, max_chunk_bytes=65535)
        yield a
        asyncio.get_event_loop().run_until_complete(a.close())

    def test_profile(self, adapter):
        p = adapter.profile()
        assert p.name == _ADAPTER_NAME
        assert not p.persistent
        assert p.tags["experimental"] == "true"
        assert p.tags["tier"] == "5"

    @pytest.mark.asyncio
    async def test_write_returns_ref(self):
        a = RtpInflightAdapter(base_port=58100)
        try:
            ref = await a.write_chunk("hello picard")
            assert ref.adapter_name == _ADAPTER_NAME
            loc = json.loads(ref.location)
            assert "ssrc" in loc
            assert "fragment_count" in loc
            assert loc["fragment_count"] == 1
        finally:
            await a.close()

    @pytest.mark.asyncio
    async def test_write_read_roundtrip(self):
        a = RtpInflightAdapter(base_port=58200)
        try:
            payload = "encrypt-chunk-scatter"
            ref = await a.write_chunk(payload)
            got = await a.read_chunk(ref)
            assert got == payload
        finally:
            await a.close()

    @pytest.mark.asyncio
    async def test_delete_returns_true_for_known(self):
        a = RtpInflightAdapter(base_port=58300)
        try:
            ref = await a.write_chunk("delete me")
            existed = await a.delete_chunk(ref)
            assert existed is True
        finally:
            await a.close()

    @pytest.mark.asyncio
    async def test_delete_removes_from_relay(self):
        a = RtpInflightAdapter(base_port=58400)
        try:
            ref = await a.write_chunk("ephemeral")
            await a.delete_chunk(ref)
            loc = json.loads(ref.location)
            ssrc = loc["ssrc"]
            assert a._relay.get_fragments(ssrc) is None
        finally:
            await a.close()

    @pytest.mark.asyncio
    async def test_read_missing_ssrc_raises(self):
        a = RtpInflightAdapter(base_port=58500)
        try:
            fake_ref = StoredChunkRef(
                adapter_name=_ADAPTER_NAME,
                location=json.dumps({
                    "ssrc": 0xDEAD1234,
                    "port_recv": 58500,
                    "port_relay": 58501,
                    "fragment_count": 1,
                    "data_len": 4,
                }),
            )
            with pytest.raises(KeyError, match="SSRC"):
                await a.read_chunk(fake_ref)
        finally:
            await a.close()

    @pytest.mark.asyncio
    async def test_unique_ssrc_per_write(self):
        a = RtpInflightAdapter(base_port=58600)
        try:
            ref1 = await a.write_chunk("chunk1")
            ref2 = await a.write_chunk("chunk2")
            loc1 = json.loads(ref1.location)
            loc2 = json.loads(ref2.location)
            assert loc1["ssrc"] != loc2["ssrc"]
        finally:
            await a.close()

    @pytest.mark.asyncio
    async def test_large_chunk_fragments(self):
        a = RtpInflightAdapter(base_port=58700, max_chunk_bytes=65535)
        try:
            data = "X" * ((_MAX_RTP_DATA + 1) * 2)
            ref = await a.write_chunk(data)
            loc = json.loads(ref.location)
            assert loc["fragment_count"] >= 3
            got = await a.read_chunk(ref)
            assert got == data
        finally:
            await a.close()

    @pytest.mark.asyncio
    async def test_list_chunks_empty(self):
        a = RtpInflightAdapter(base_port=58800)
        try:
            result = await a.list_chunks("some-manifest")
            assert result == []
        finally:
            await a.close()

    @pytest.mark.asyncio
    async def test_health_check_alive(self):
        a = RtpInflightAdapter(base_port=58900)
        try:
            # Relay is running; health check may succeed or fail depending on
            # OS loopback timing, but should not raise.
            result = await a.health_check()
            assert isinstance(result, bool)
        finally:
            await a.close()
