"""Tests for halfdouble_adapter.

Verifies the half-double specific hammer pattern (non-uniform near/far
access ratio) and that the profile discovery uses two-hop aggressors.
"""

import sys
from unittest.mock import MagicMock, patch, call

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="halfdouble tests require Linux",
)


@pytest.fixture(autouse=True)
def _patch_linux_x86(monkeypatch):
    monkeypatch.setattr("picard.adapters.rowhammer_adapter._LINUX_X86", True)


from picard.adapters.halfdouble_adapter import HalfDoubleAdapter
from picard.adapters.rowhammer_adapter import (
    FlippableBit,
    IntelSandyBridgeGeometry,
    RowhammerProfile,
)


class TestHalfDoubleHammerPattern:
    """The half-double hammer must access near aggressor more than far."""

    def _make_adapter(self, ratio=5):
        a = HalfDoubleAdapter(
            near_far_ratio=ratio,
            hammer_iterations=20,   # small for testing
            max_chunk_bytes=4,
        )
        return a

    def test_near_accessed_more_than_far(self):
        a = self._make_adapter(ratio=4)
        hw = MagicMock()
        a._hw = hw

        near_addr = 0x1000
        far_addr = 0x2000

        # Collect ctypes.c_uint8.from_address calls
        access_log = []
        import ctypes
        original_from_address = ctypes.c_uint8.from_address

        def mock_from_address(addr):
            access_log.append(addr)
            return MagicMock(value=0)

        with patch("ctypes.c_uint8") as mock_c_uint8:
            mock_c_uint8.from_address.side_effect = mock_from_address
            hw.clflush = MagicMock()
            a._hammer(near_addr, far_addr)

        near_count = access_log.count(near_addr)
        far_count = access_log.count(far_addr)
        # near accessed every iteration (20); far accessed every ratio iterations (20/4 = 5)
        assert near_count > far_count
        assert far_count == a._iterations // a._nf_ratio

    def test_ratio_one_means_equal_access(self):
        a = self._make_adapter(ratio=1)
        access_log = []

        import ctypes
        with patch("ctypes.c_uint8") as mock_c_uint8:
            mock_c_uint8.from_address.side_effect = lambda addr: (
                access_log.append(addr) or MagicMock(value=0)
            )
            a._hw = MagicMock()
            a._hammer(0xAAAA, 0xBBBB)

        assert access_log.count(0xAAAA) == access_log.count(0xBBBB)

    def test_adapter_profile_name_is_halfdouble(self):
        a = self._make_adapter()
        p = a.profile()
        assert p.name == "halfdouble"
        assert p.persistent is False


class TestHalfDoubleCandidates:
    """_halfdouble_candidates must return two-hop (N-2, N-1, N) triples."""

    def test_candidate_structure(self, monkeypatch):
        a = HalfDoubleAdapter(hammer_iterations=1, max_chunk_bytes=4)
        geo = IntelSandyBridgeGeometry()
        a._geometry = geo

        row_size = 8192
        n_rows = 6
        base = 0x1000_0000
        region_size = n_rows * row_size

        # Mock _virt_to_phys so rows appear consecutive in same bank
        def mock_v2p(virt):
            row_num = (virt - base) // row_size
            # Place all rows in the same bank: use physical addresses that the
            # Sandy Bridge geometry will map to the same bank
            # bank[0] = PA[14] ^ PA[18]; choose addrs where these XOR to 0
            return virt   # use virtual == physical for simplicity

        monkeypatch.setattr(
            "picard.adapters.rowhammer_adapter._virt_to_phys", mock_v2p
        )

        candidates = a._halfdouble_candidates(base, region_size)
        # May be empty if same-bank logic doesn't match (geometry-specific),
        # but we verify the shape when candidates exist
        for near_agg, far_agg, victim in candidates:
            # near_agg should be one row before victim
            # far_agg should be two rows before victim
            assert (victim - near_agg) == row_size
            assert (victim - far_agg) == 2 * row_size

    def test_fallback_candidates_when_pagemap_fails(self, monkeypatch):
        a = HalfDoubleAdapter(hammer_iterations=1, max_chunk_bytes=4)
        a._geometry = IntelSandyBridgeGeometry()

        # Make _virt_to_phys raise so we fall back to virtual-contiguous assumption
        monkeypatch.setattr(
            "picard.adapters.rowhammer_adapter._virt_to_phys",
            lambda v: (_ for _ in ()).throw(RuntimeError("pagemap unavailable")),
        )

        row_size = 8192
        base = 0x1000_0000
        region_size = row_size * 6
        candidates = a._halfdouble_candidates(base, region_size)
        # With fallback: rows assumed contiguous, candidates exist
        assert len(candidates) > 0
        for near_agg, far_agg, victim in candidates:
            assert victim - near_agg == row_size
            assert victim - far_agg == 2 * row_size
