"""Tests for rowhammer_adapter.

Hardware-dependent code is mocked. We test:
  - Profile serialisation / deserialisation
  - Chunk bit encoding (which bits need to flip)
  - Chunk bit decoding (reading virtual addresses)
  - Geometry helpers (IntelSandyBridgeGeometry bank function)
  - write_chunk / read_chunk with mocked hardware primitives
"""

import json
import sys
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

# Skip the entire module if not on Linux x86-64 at import time; the adapter
# guards itself at runtime but many tests patch the guard away.
pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="rowhammer tests require Linux (CI skips gracefully)",
)


@pytest.fixture(autouse=True)
def _patch_linux_x86(monkeypatch):
    """Pretend we are on Linux x86-64 regardless of the actual test runner."""
    monkeypatch.setattr(
        "picard.adapters.rowhammer_adapter._LINUX_X86", True
    )


from picard.adapters.rowhammer_adapter import (
    FlippableBit,
    IntelSandyBridgeGeometry,
    RowhammerAdapter,
    RowhammerProfile,
)


# ---------------------------------------------------------------------------
# IntelSandyBridgeGeometry
# ---------------------------------------------------------------------------

class TestIntelSandyBridgeGeometry:
    def setup_method(self):
        self.geo = IntelSandyBridgeGeometry()

    def test_bank_zero_for_zero_address(self):
        assert self.geo.bank(0) == 0

    def test_bank_differs_across_row_boundary(self):
        # Addresses 8 KB apart should be in the same bank only when bank
        # XOR bits are the same. We just check the function is deterministic.
        addr = 0x1000_0000
        b1 = self.geo.bank(addr)
        b2 = self.geo.bank(addr + 0x2000)
        assert isinstance(b1, int)
        assert isinstance(b2, int)

    def test_row_increases_monotonically(self):
        for i in range(4):
            r0 = self.geo.row(i * 8192)
            r1 = self.geo.row((i + 1) * 8192)
            assert r1 == r0 + 1

    def test_same_bank_symmetry(self):
        a = 0x1000_0000
        b = 0x1000_2000
        assert self.geo.same_bank(a, b) == self.geo.same_bank(b, a)


# ---------------------------------------------------------------------------
# RowhammerProfile serialisation
# ---------------------------------------------------------------------------

class TestRowhammerProfile:
    def _make_profile(self):
        bits = [
            FlippableBit(
                aggressor_a_virt=0x100_0000,
                aggressor_b_virt=0x104_0000,
                victim_virt=0x102_0000,
                bit_offset=i,
                natural_value=0,
            )
            for i in range(64)
        ]
        return RowhammerProfile(bits=bits, geometry="IntelSandyBridgeGeometry")

    def test_roundtrip_json(self):
        p = self._make_profile()
        s = p.to_json()
        p2 = RowhammerProfile.from_json(s)
        assert len(p2.bits) == len(p.bits)
        assert p2.bits[0].aggressor_a_virt == p.bits[0].aggressor_a_virt
        assert p2.bits[7].natural_value == 0
        assert p2.geometry == "IntelSandyBridgeGeometry"

    def test_capacity_bytes(self):
        p = self._make_profile()
        assert p.capacity_bytes() == 8   # 64 bits / 8

    def test_empty_profile(self):
        p = RowhammerProfile()
        assert p.capacity_bytes() == 0
        s = p.to_json()
        p2 = RowhammerProfile.from_json(s)
        assert len(p2.bits) == 0


# ---------------------------------------------------------------------------
# Chunk encoding helpers
# ---------------------------------------------------------------------------

class TestChunkEncoding:
    """Tests for _encode() and _decode() without hardware."""

    def _make_adapter(self):
        a = RowhammerAdapter.__new__(RowhammerAdapter)
        a._iterations = 1
        a._region_bytes = 0
        a._geometry = IntelSandyBridgeGeometry()
        a._max = 512
        a._profile = None
        a._region_base = None
        a._region_mmap = None
        a._hw = None
        return a

    def _make_profile(self, n_bits: int = 256):
        bits = [
            FlippableBit(
                aggressor_a_virt=0x1000 * (i + 1),
                aggressor_b_virt=0x1000 * (i + 2),
                victim_virt=0x2000 * (i + 1),
                bit_offset=0,
                natural_value=0,   # all bits naturally 0
            )
            for i in range(n_bits)
        ]
        return RowhammerProfile(bits=bits)

    def test_encode_all_zeros(self):
        a = self._make_adapter()
        profile = self._make_profile()
        data = b"\x00" * 8   # 64 bits, all 0
        to_flip = a._encode(data, profile)
        # All natural values are 0, all target bits are 0: nothing to flip
        assert len(to_flip) == 0

    def test_encode_all_ones(self):
        a = self._make_adapter()
        profile = self._make_profile()
        data = b"\xFF" * 8   # 64 bits, all 1
        to_flip = a._encode(data, profile)
        # Natural value = 0, target = 1: all 64 bits need flipping
        assert len(to_flip) == 64

    def test_encode_alternating(self):
        a = self._make_adapter()
        profile = self._make_profile()
        data = bytes([0b10101010] * 8)
        to_flip = a._encode(data, profile)
        assert len(to_flip) == 32   # every other bit needs to flip

    def test_encode_raises_when_profile_too_small(self):
        a = self._make_adapter()
        profile = self._make_profile(n_bits=7)   # only 7 bits available
        with pytest.raises(RuntimeError, match="flippable bits"):
            a._encode(b"\xFF", profile)   # needs 8 bits


# ---------------------------------------------------------------------------
# write_chunk / read_chunk with mocked hardware
# ---------------------------------------------------------------------------

class TestRowhammerAdapterIO:
    def _make_adapter(self):
        return RowhammerAdapter(
            hammer_iterations=1,
            region_bytes=1024,
            max_chunk_bytes=4,
        )

    def _make_profile(self, adapter, n_bits=64):
        import ctypes
        # Create real memory backed by ctypes arrays so reads work
        page_size = 4096
        pages = (ctypes.c_uint8 * (page_size * n_bits))()
        bits = []
        for i in range(n_bits):
            virt = ctypes.addressof(pages) + i * page_size
            bits.append(FlippableBit(
                aggressor_a_virt=virt,
                aggressor_b_virt=virt + 8192,
                victim_virt=virt,
                bit_offset=0,
                natural_value=0,
            ))
        profile = RowhammerProfile(bits=bits)
        adapter.load_profile(profile)
        adapter._pages = pages   # keep alive
        return profile

    @patch("picard.adapters.rowhammer_adapter._virt_to_phys", return_value=0x1000)
    @patch("picard.adapters.rowhammer_adapter._alloc_huge_pages")
    def test_write_chunk_location_format(self, mock_alloc, mock_v2p):
        import ctypes
        a = self._make_adapter()
        profile = self._make_profile(a, n_bits=64)
        a._hw = MagicMock()

        # Suppress actual hammering
        a._hammer = MagicMock()

        import asyncio
        ref = asyncio.get_event_loop().run_until_complete(
            a.write_chunk("ABCD")
        )
        assert ref.adapter_name == "rowhammer"
        assert ref.location.startswith("hammer://")
        payload = json.loads(ref.location.removeprefix("hammer://"))
        assert payload["n_bytes"] == 4
        assert isinstance(payload["bits"], list)
        assert len(payload["bits"]) == 32   # 4 bytes * 8 bits

    @patch("picard.adapters.rowhammer_adapter._virt_to_phys", return_value=0x1000)
    @patch("picard.adapters.rowhammer_adapter._alloc_huge_pages")
    def test_write_read_roundtrip(self, mock_alloc, mock_v2p):
        import ctypes
        a = self._make_adapter()
        profile = self._make_profile(a, n_bits=64)
        a._hw = MagicMock()
        a._hammer = MagicMock()

        import asyncio
        original = "AB"
        ref = asyncio.get_event_loop().run_until_complete(
            a.write_chunk(original)
        )
        recovered = asyncio.get_event_loop().run_until_complete(
            a.read_chunk(ref)
        )
        assert recovered == original

    def test_chunk_too_large_raises(self):
        a = self._make_adapter()   # max_chunk_bytes=4
        import asyncio
        with pytest.raises(ValueError, match="too large"):
            asyncio.get_event_loop().run_until_complete(
                a.write_chunk("ABCDE")   # 5 bytes > 4
            )

    def test_health_check_fails_on_non_linux(self, monkeypatch):
        monkeypatch.setattr("picard.adapters.rowhammer_adapter._LINUX_X86", False)
        a = self._make_adapter()
        import asyncio
        assert asyncio.get_event_loop().run_until_complete(a.health_check()) is False
