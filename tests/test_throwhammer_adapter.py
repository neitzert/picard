"""Tests for throwhammer_adapter.

RDMA hardware is mocked. We test:
  - location serialisation format
  - chunk encode/decode cycle with a mock RDMA connection
  - health check fails when host unreachable
  - QP parameter packing/unpacking roundtrip
"""

import json
import struct
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="throwhammer tests require Linux",
)


@pytest.fixture(autouse=True)
def _patch_linux(monkeypatch):
    monkeypatch.setattr("picard.adapters.throwhammer_adapter._LINUX", True)


from picard.adapters.throwhammer_adapter import (
    RdmaQpParams,
    RdmaRemoteMemory,
    ThrowhammerAdapter,
    _pack_qp_params,
    _unpack_qp_params,
)


# ---------------------------------------------------------------------------
# QP parameter pack/unpack
# ---------------------------------------------------------------------------

class TestQpParamsPacking:
    def test_roundtrip_without_memory(self):
        params = RdmaQpParams(lid=0x01, qpn=0x1234, psn=0x777, gid=b"\x00" * 16)
        packed = _pack_qp_params(params, None)
        unpacked_params, unpacked_mem = _unpack_qp_params(packed)
        assert unpacked_params.lid == params.lid
        assert unpacked_params.qpn == params.qpn
        assert unpacked_params.psn == params.psn
        assert unpacked_mem is None

    def test_roundtrip_with_memory(self):
        params = RdmaQpParams(lid=0x02, qpn=0x5678, psn=0x999, gid=b"\xAB" * 16)
        mem = RdmaRemoteMemory(remote_addr=0xDEAD_BEEF, rkey=0xCAFE, length=4096)
        packed = _pack_qp_params(params, mem)
        unpacked_params, unpacked_mem = _unpack_qp_params(packed)
        assert unpacked_params.qpn == params.qpn
        assert unpacked_mem is not None
        assert unpacked_mem.remote_addr == mem.remote_addr
        assert unpacked_mem.rkey == mem.rkey
        assert unpacked_mem.length == mem.length

    def test_gid_truncated_to_16_bytes(self):
        params = RdmaQpParams(lid=1, qpn=1, psn=1, gid=b"\x11" * 32)
        packed = _pack_qp_params(params, None)
        unpacked, _ = _unpack_qp_params(packed)
        assert len(unpacked.gid) == 16


# ---------------------------------------------------------------------------
# ThrowhammerAdapter IO with mock RDMA connection
# ---------------------------------------------------------------------------

def _make_mock_connection(remote_mem: RdmaRemoteMemory):
    """Return a mock RdmaConnection that reads back written bytes."""
    import ctypes
    buf = (ctypes.c_uint8 * remote_mem.length)()
    buf_base = ctypes.addressof(buf)

    conn = MagicMock()
    conn._buf = buf   # keep alive

    async def mock_rdma_read(remote_addr, rkey, length):
        offset = remote_addr - remote_mem.remote_addr
        return bytes(buf[offset:offset + length])

    async def mock_rdma_write(remote_addr, rkey, data):
        offset = remote_addr - remote_mem.remote_addr
        for i, b in enumerate(data):
            buf[offset + i] = b

    async def mock_get_remote_memory_params():
        return remote_mem

    async def mock_close():
        pass

    conn.rdma_read = mock_rdma_read
    conn.rdma_write = mock_rdma_write
    conn.get_remote_memory_params = mock_get_remote_memory_params
    conn.close = mock_close
    return conn


class TestThrowhammerAdapterIO:
    def _make_adapter(self):
        return ThrowhammerAdapter(
            target_host="10.0.0.1",
            hammer_iterations=1,
            max_chunk_bytes=4,
        )

    async def test_write_chunk_returns_throwhammer_location(self):
        a = self._make_adapter()
        # Pre-load a profile so we skip the remote profiling call
        remote_mem = RdmaRemoteMemory(
            remote_addr=0x1000_0000,
            rkey=0xABCD,
            length=1024 * 1024,
        )
        bits = [
            {
                "agg_a": 0x1000_0000 + i * 8192,
                "agg_b": 0x1000_0000 + (i + 2) * 8192,
                "victim_addr": 0x1000_0000 + (i + 1) * 8192,
                "bit_offset": 0,
                "natural_value": 0,
            }
            for i in range(64)
        ]
        a._profile = bits
        a._remote_mem = remote_mem

        conn = _make_mock_connection(remote_mem)
        a._open_rdma_connection = AsyncMock(return_value=conn)
        a._close_rdma_connection = AsyncMock()
        a._hammer_remote_rows = AsyncMock()

        ref = await a.write_chunk("AB")
        assert ref.adapter_name == "throwhammer"
        assert ref.location.startswith("throwhammer://")
        payload = json.loads(ref.location.removeprefix("throwhammer://"))
        assert payload["host"] == "10.0.0.1"
        assert payload["n_bytes"] == 2
        assert len(payload["bits"]) == 16   # 2 bytes * 8 bits

    async def test_health_check_fails_on_non_linux(self, monkeypatch):
        monkeypatch.setattr("picard.adapters.throwhammer_adapter._LINUX", False)
        a = self._make_adapter()
        result = await a.health_check()
        assert result is False

    async def test_health_check_fails_when_host_unreachable(self):
        a = ThrowhammerAdapter(
            target_host="192.0.2.255",   # documentation range, unreachable
            hammer_iterations=1,
            max_chunk_bytes=4,
        )
        result = await a.health_check()
        assert result is False

    def test_profile_name_is_throwhammer(self):
        a = self._make_adapter()
        assert a.profile().name == "throwhammer"
        assert a.profile().persistent is False
